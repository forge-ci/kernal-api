import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from measure_compiler_policy import edit_source  # pylint: disable=wrong-import-position


class SharedPolicyEditTests(unittest.TestCase):
    def test_edits_the_one_shared_request_key_anchor(self):
        source = 'let raw = ["-MD", "-MF-", "source.c"].map(String::from);\n'
        first = edit_source(source, 0, 1)
        self.assertIn('"source-1.c"', first)
        self.assertEqual(edit_source(first, 1, 2).count('"source-2.c"'), 1)

    def test_rejects_missing_or_ambiguous_anchor(self):
        with self.assertRaises(ValueError):
            edit_source("", 0, 1)
        source = 'let raw = ["-MD", "-MF-", "source.c"].map(String::from);\n' * 2
        with self.assertRaises(ValueError):
            edit_source(source, 0, 1)
