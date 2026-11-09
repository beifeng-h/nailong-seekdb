"""Compile the actual IK headers and compare against the pinned pre-change code.

Only log emission is suppressed in this isolated test. The production UTF-8
 decoder and Unicode predicates are compiled unchanged; this is not a SQL test.
"""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[3]
HEADER = 'src/storage/fts/ik/ob_ik_char_util.h'
REFERENCE = 'fe876f8f805969970621a18a1cf812ff322fc78c'
REFERENCE_SHA256 = 'd26f03bfb9eb494a5259e1345744369473d7de0b781831e0492d16add2db6ca8'

HARNESS = r'''
#include "lib/charset/ob_charset.h"
#include "lib/charset/ob_charset_string_helper.h"
#include "lib/charset/ob_ctype.h"
#include "lib/ob_errno.h"
#include "lib/oblog/ob_log_module.h"
#include "lib/utility/ob_macro_utils.h"
#include "storage/fts/utils/unicode_utils.h"
#include <cstdint>
#include <type_traits>
#include <cstdio>
#include <cstdlib>
#undef STORAGE_FTS_LOG
#define STORAGE_FTS_LOG(...)
#define private public
#include "storage/fts/ik/ob_ik_char_util.h"
#undef _OCEANBASE_STORAGE_FTS_IK_OB_IK_CHAR_UTIL_H_
#define ObFTCharUtil ReferenceFTCharUtil
#include "reference.h"
#undef ObFTCharUtil
#undef private
using namespace oceanbase::storage;
using namespace oceanbase::common;
static uint64_t checks = 0;
template<ObCharsetType C> static void check(const char *p, uint8_t length) {
  ObFTCharUtil::CharType actual = ObFTCharUtil::CharType::CHINESE;
  ReferenceFTCharUtil::CharType expected = ReferenceFTCharUtil::CharType::CHINESE;
  const int a = ObFTCharUtil::do_classify<C>(p, length, actual);
  const int b = ReferenceFTCharUtil::do_classify<C>(p, length, expected);
  ++checks;
  if (a != b || static_cast<int>(actual) != static_cast<int>(expected)) {
    std::fprintf(stderr, "Mismatch charset=%d len=%u ret=%d/%d type=%d/%d check=%llu\n",
      static_cast<int>(C), length, a, b, static_cast<int>(actual),
      static_cast<int>(expected), static_cast<unsigned long long>(checks));
    std::exit(1);
  }
}
static int encode(uint32_t cp, char *out) {
  if (cp <= 0x7f) { out[0] = cp; return 1; }
  if (cp <= 0x7ff) { out[0]=0xc0|(cp>>6); out[1]=0x80|(cp&63); return 2; }
  if (cp <= 0xffff) { out[0]=0xe0|(cp>>12); out[1]=0x80|((cp>>6)&63); out[2]=0x80|(cp&63); return 3; }
  out[0]=0xf0|(cp>>18); out[1]=0x80|((cp>>12)&63); out[2]=0x80|((cp>>6)&63); out[3]=0x80|(cp&63); return 4;
}
int main() {
  char data[256] = {};
  // Every scalar, plus encoded surrogate values: exact length, each truncation,
  // and trailing bytes. Includes NUL, fullwidth forms and all CJK boundaries.
  for (uint32_t cp=0; cp<=0x10ffff; ++cp) {
    const int len=encode(cp,data);
    for (int n=0; n<=len+1; ++n) check<CHARSET_UTF8MB4>(data,n);
  }
  // Exhaustive one/two-byte prefixes and every UTF16 code unit, both byte orders.
  for (uint32_t bits=0; bits<=0xffff; ++bits) {
    data[0]=bits&255; data[1]=bits>>8; data[2]=0; data[3]=0xff;
    for (int n=0; n<=4; ++n) {
      check<CHARSET_UTF8MB4>(data,n);
      check<CHARSET_UTF16>(data,n);
      check<CHARSET_UTF16LE>(data,n);
    }
  }
  uint32_t state=0x718293ab;
  for (unsigned sample=0; sample<100000; ++sample) {
    for (unsigned i=0; i<sizeof(data); ++i) {
      state ^= state<<13; state ^= state>>17; state ^= state<<5;
      data[i]=state&255;
    }
    for (unsigned n=0; n<8; ++n) check<CHARSET_UTF8MB4>(data,n);
    check<CHARSET_UTF8MB4>(data,255);
    check<CHARSET_UTF16>(data,255);
    check<CHARSET_UTF16LE>(data,255);
  }
  for (unsigned n=0; n<256; ++n) {
    check<CHARSET_UTF8MB4>(nullptr,n);
    check<CHARSET_UTF16>(nullptr,n);
    check<CHARSET_UTF16LE>(nullptr,n);
  }
  std::printf("IK_DIFFERENTIAL checks=%llu mismatches=0 actual_headers=true actual_decoder=true actual_unicode_predicates=true\n",
    static_cast<unsigned long long>(checks));
}
'''


class IkClassificationContract(unittest.TestCase):
    def test_dispatch_reads_one_tuple_and_preserves_segmenter_order(self):
        parser = (REPO / 'src/storage/fts/ob_ik_ft_parser.cpp').read_text()
        start = parser.index('int ObIKFTParser::process_one_char(')
        end = parser.index('int ObIKFTParser::process_next_batch()', start)
        dispatch = parser[start:end]
        self.assertIn('OB_SUCC(ret) && iter != segmenters_.end()', dispatch)
        self.assertIn('OB_FAIL((*iter)->do_process(ctx, ch, char_len, type))', dispatch)
        batch = parser[end:parser.index('int ObIKFTParserDesc::segment(', end)]
        self.assertLess(batch.index('ctx_->current_char(ch, char_len)'),
                        batch.index('process_one_char(*ctx_, ch, char_len, type)'))
        self.assertLess(batch.index('ctx_->current_char_type(type)'),
                        batch.index('process_one_char(*ctx_, ch, char_len, type)'))
        self.assertLess(batch.index('process_one_char(*ctx_, ch, char_len, type)'),
                        batch.index('ctx_->step_next()'))
        for name in ('letter', 'quantifier', 'cjk', 'surrogate'):
            source = (REPO / ('src/storage/fts/ik/ob_ik_' + name + '_processor.cpp')).read_text()
            for forbidden in ('step_next(', 'reset_resource(', 'ctx.init('):
                self.assertNotIn(forbidden, source)
        init = parser[parser.index('int ObIKFTParser::init_segmenter('):]
        pushes = [init.index('segmenters_.push_back(' + x + ')')
                  for x in ('letter_seg', 'cnqsg', 'cjksg', 'surrogate_seg')]
        self.assertEqual(pushes, sorted(pushes))

    def test_actual_header_differential(self):
        compiler = shutil.which('g++')
        self.assertIsNotNone(compiler, 'g++ is required for actual-header differential validation')
        reference = subprocess.check_output(['git', '-C', str(REPO), 'show', REFERENCE + ':' + HEADER])
        self.assertEqual(hashlib.sha256(reference).hexdigest(), REFERENCE_SHA256)
        # No decoder/helper/predicate semantics are silently changed by this test.
        with tempfile.TemporaryDirectory(prefix='fts-ik-differential-') as temp:
            root = Path(temp)
            (root / 'reference.h').write_bytes(reference)
            (root / 'main.cpp').write_text(HARNESS, encoding='utf-8')
            command = [compiler, '-std=gnu++17', '-O2', '-ffunction-sections', '-fdata-sections',
                '-I' + str(REPO), '-I' + str(REPO / 'src'), '-I' + str(REPO / 'src/oblib'),
                '-I' + str(root), str(root / 'main.cpp'),
                str(REPO / 'src/storage/fts/utils/unicode_utils.cpp'),
                '-Wl,--gc-sections', '-o', str(root / 'compare')]
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
            result = subprocess.run([str(root / 'compare')], check=True, capture_output=True,
                                    text=True, timeout=120)
            self.assertRegex(result.stdout, r'IK_DIFFERENTIAL checks=\d+ mismatches=0 actual_headers=true')
            print(result.stdout.strip())


if __name__ == '__main__':
    unittest.main(verbosity=2)
