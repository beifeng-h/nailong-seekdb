import json
from pathlib import Path
import unittest
from unittest import mock

import fts_ddl_row_cache_validation as validation


def index_output(phase, wrong=False):
    ids = [('A', '1'), ('B', '2'), ('C', '3'), ('D', '4'), ('E', '5')]
    if phase == 'mutated':
        ids = [row for row in ids if row[1] != '2']
    long_count = 3300 if phase == 'built' else 1004
    if wrong:
        long_count += 1
    postings = [('中', 'A', str(long_count), str(long_count)), ('华', 'C', '65', '65')]
    postings += [(word, 'E', '1', '300') for word in validation.distinct_words()]
    if phase == 'built':
        postings.append(('中', 'B', '150', '150'))
    rows = [('map', *row) for row in ids]
    rows += [(layout, *row) for layout in ('13', '14') for row in postings]
    rows += [('hits', '1')] + ([('hits', '2')] if phase == 'built' else [])
    return ''.join('\t'.join(row) + '\n' for row in rows)


class EqualityGuardTests(unittest.TestCase):
    def result(self, matrix='tokens', beng=None, ik=None):
        beng = {'rows': [1, 2]} if beng is None else beng
        ik = {'built': {'postingsPerLayout': 3}} if ik is None else ik
        with mock.patch.object(validation, 'corrected_equality_work', return_value={'canonical': '01'}), \
             mock.patch.object(validation.previous, 'matrix_work', return_value={'canonical': matrix, 'rows': 28}), \
             mock.patch.object(validation.targeted, 'run', return_value={'canonical': beng, 'statements': 10}), \
             mock.patch.object(validation, 'index_work', return_value=ik) as index:
            result = validation.equality_work(None, Path('unused'))
        index.assert_called_once_with(None, Path('unused'), 'baseline')
        return result['canonical']

    def test_all_sql_outputs_are_part_of_comparison(self):
        reference = self.result()
        self.assertNotEqual(reference, self.result(matrix='changed'))
        self.assertNotEqual(reference, self.result(beng={'rows': [1]}))
        self.assertNotEqual(reference, self.result(ik={'built': {'postingsPerLayout': 2}}))

    def test_both_ik_index_layouts_reject_changed_postings(self):
        catalog = '\n'.join(('aux\t13\tidx_a', 'aux\t14\tidx_b',
                             'col\t0\t__doc_id_1', 'col\t0\t__word_segment_1',
                             'col\t0\t__word_count_1', 'col\t0\t__doc_length_1')) + '\n'

        def run(wrong):
            def sql(call, out, label, query):
                if label == 'ik-batch-many-tokenize':
                    return json.dumps({'tokens': [{word: 1} for word in validation.distinct_words()],
                                       'doc_len': 300})
                if label in ('ik-batch-index-setup', 'ik-batch-index-mutate'):
                    return ''
                if label == 'ik-batch-table-id':
                    return '123\n'
                if label == 'ik-batch-catalog':
                    return catalog
                return index_output(label.removeprefix('ik-batch-index-'), wrong)
            with mock.patch.object(validation.driver, 'sql', side_effect=sql):
                return validation.index_work(None, Path('unused'), 'baseline')

        self.assertEqual(run(False)['built']['longFrequency'], 3300)
        with self.assertRaisesRegex(RuntimeError, 'postings'):
            run(True)


if __name__ == '__main__':
    unittest.main()
