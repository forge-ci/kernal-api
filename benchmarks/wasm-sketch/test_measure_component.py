import json
import unittest
from pathlib import Path

from measure_component import edit_source, validate_output
from measure_core import build_rss, edit_summary


class ComponentMeasurementTests(unittest.TestCase):
    def test_optional_component_rss_uses_the_shared_positive_kib_parser(self):
        self.assertEqual(build_rss("64\n"), 64 * 1024)
        for invalid in ("", "0", "1.5", "64\n65"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_rss(invalid)

    def test_recorded_diagnostic_has_ten_distinct_compiled_edits(self):
        for profile in ("debug", "release", "release-rss"):
            with self.subTest(profile=profile):
                path = Path(__file__).parent / f"results/component-{profile}-diagnostic.json"
                record = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(record["status"], "complete-diagnostic-only")
                if profile != "release-rss":
                    self.assertEqual(record["encoder_profile"], profile)
                self.assertEqual(len(record["samples"]), 12)
                self.assertEqual(edit_summary(record["samples"][2:]), record["edit_summary"])
                for sample in record["samples"]:
                    self.assertGreater(sample["module_bytes"], 0)
                    self.assertGreater(sample["encode_and_compile_command_ns"], 0)
                if profile == "release-rss":
                    peaks = [sample["build_peak_rss_bytes"] for sample in record["samples"]]
                    self.assertTrue(all(type(peak) is int and peak > 0 for peak in peaks))
                    self.assertEqual(record["build_memory"]["peak_rss_bytes"], max(peaks))
                    self.assertIsNone(record["peak_compiler_rss_bytes"])

    def test_edit_changes_exactly_one_runtime_limit(self):
        source = "if total > 64 * 1024 * 1024 {"
        self.assertEqual(edit_source(source, 64, 65), "if total > 65 * 1024 * 1024 {")
        for invalid in ("no anchor", source * 2):
            with self.assertRaises(ValueError):
                edit_source(invalid, 64, 65)

    def test_runner_edits_the_default_component_policy_not_dispatch_glue(self):
        runner = Path(__file__).parent / "measure_component.py"
        self.assertIn('guest / "src/legacy.rs"', runner.read_text(encoding="utf-8"))

    def test_requires_encoding_and_engine_compilation_of_exact_size(self):
        output = (
            "validated component: 123 bytes; two kernel imports; not executed\n"
            "Wasmtime 45 component compilation passed; not instantiated or executed\n"
        )
        validate_output(output, 123)
        for invalid in (
            "",
            output.splitlines()[0],
            output.replace("123", "124"),
            output + "unexpected output\n",
            output.replace("two kernel imports", "one kernel import"),
        ):
            with self.assertRaises(ValueError):
                validate_output(invalid, 123)


if __name__ == "__main__":
    unittest.main()
