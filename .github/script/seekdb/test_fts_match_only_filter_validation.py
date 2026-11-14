import unittest

import fts_match_only_filter_validation as validation


class ProductScopeTests(unittest.TestCase):
    def setUp(self):
        self.original = (b'  sr_iter_param_.relevance_proj_expr_ = ir_ctdef_->relevance_proj_col_;\n'
                         + validation.OLD_FILTER + b'\n')
        self.candidate = self.original.replace(validation.OLD_FILTER, validation.NEW_FILTER)

    def test_accepts_only_the_guarded_filter_change(self):
        validation.verify_product(self.original, self.candidate)

    def test_unchanged_filter_is_rejected(self):
        with self.assertRaises(RuntimeError):
            validation.verify_product(self.original, self.original)

    def test_unconditional_filter_removal_is_rejected(self):
        changed = self.original.replace(validation.OLD_FILTER,
                                        b'  sr_iter_param_.filter_expr_ = nullptr;')
        with self.assertRaises(RuntimeError):
            validation.verify_product(self.original, changed)

    def test_projection_removal_is_rejected(self):
        changed = self.candidate.replace(b'ir_ctdef_->relevance_proj_col_', b'nullptr')
        with self.assertRaises(RuntimeError):
            validation.verify_product(self.original, changed)


if __name__ == '__main__':
    unittest.main()
