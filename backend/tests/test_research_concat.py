"""engine.research_data.concat: no pandas FutureWarning for a piece with an all-blank column, and no rows or columns lost."""
import unittest
import warnings

import numpy as np
import pandas as pd

from engine.research_data import concat


class ConcatTest(unittest.TestCase):
    def test_all_blank_column_gives_no_warning_and_keeps_everything(self):
        a = pd.DataFrame({"x": [1.0, 2.0], "oi": [np.nan, np.nan]})
        b = pd.DataFrame({"x": [3.0], "oi": [7.0]})
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = concat([a, None, pd.DataFrame(), b])
        self.assertEqual(list(out.columns), ["x", "oi"])
        self.assertEqual(out["x"].tolist(), [1.0, 2.0, 3.0])
        self.assertEqual(out["oi"].iloc[2], 7.0)
        self.assertTrue(out["oi"].iloc[:2].isna().all())

    def test_nothing_gives_the_standard_empty_frame(self):
        self.assertTrue(concat([None, pd.DataFrame()]).empty)


if __name__ == "__main__":
    unittest.main()
