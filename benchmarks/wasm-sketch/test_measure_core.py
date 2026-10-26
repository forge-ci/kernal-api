import unittest
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from measure_core import edit_source, run, summary, validate_admission, edit_summary, build_rss


class MeasurementTests(unittest.TestCase):
    def test_build_rss_requires_one_positive_integer_in_kib(self):
        self.assertEqual(build_rss("123\n"), 123 * 1024)
        for invalid in ("", "0", "-1", "1.5", "123\n456", "Maximum RSS: 123", "True"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_rss(invalid)

    def test_measured_command_retains_separate_rss_record(self):
        with tempfile.TemporaryDirectory(prefix="kernal-measure-test-") as directory:
            root = Path(directory)
            log, rss = root / "build.log", root / "build.rss-kib"
            time_path = Path("/usr/bin/time")

            def successful_time(command, **kwargs):
                self.assertEqual(command[:7], [str(time_path), "-f", "%M", "-o", str(rss), "--", "soldr"])
                self.assertEqual(kwargs["env"]["LC_ALL"], "C")
                rss.write_text("42\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            with patch("measure_core.subprocess.run", side_effect=successful_time):
                _, output = run(["soldr", "--no-cache", "cargo", "build"], root, log, gnu_time=time_path, rss_log=rss)
            self.assertEqual(output, "ok")
            self.assertEqual(build_rss(rss.read_text(encoding="utf-8")), 42 * 1024)

    def test_recorded_diagnostics_have_distinct_edits_and_exact_summaries(self):
        results = Path(__file__).parent / "results"
        for name in ("core-debug-diagnostic.json", "core-release-diagnostic.json", "core-release-rss-diagnostic.json"):
            path = results / name
            with self.subTest(path=path.name):
                record = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(record["status"], "complete-diagnostic-only")
                self.assertEqual(len(record["samples"]), 12)
                self.assertEqual(edit_summary(record["samples"][2:]), record["edit_summary"])
                for sample in record["samples"]:
                    validate_admission(sample["admission"], sample["admission"]["module_bytes"])
                if name == "core-release-rss-diagnostic.json":
                    peaks = [sample["build_peak_rss_bytes"] for sample in record["samples"]]
                    self.assertTrue(all(type(peak) is int and peak > 0 for peak in peaks))
                    self.assertEqual(record["build_memory"]["peak_rss_bytes"], max(peaks))
                    self.assertIsNone(record["peak_compiler_rss_bytes"])

    def test_reused_artifact_cannot_be_counted_as_an_edit(self):
        samples = [
            {"wall_ns": index + 1, "source_sha256": f"source-{index}", "module_sha256": f"module-{index}"}
            for index in range(10)
        ]
        self.assertEqual(edit_summary(samples)["p50_ns"], 5.5)
        for field in ("source_sha256", "module_sha256"):
            repeated = [dict(sample) for sample in samples]
            repeated[-1][field] = repeated[0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                edit_summary(repeated)

    def test_admission_record_matches_the_actual_module_and_profile(self):
        record = {
            "schema": 1,
            "profile": "threaded-rust-v1",
            "executed": False,
            "module_bytes": 123,
            "compiler_setup_ns": 0,
            "admission_ns": 42,
        }
        self.assertEqual(validate_admission(record, 123), record)
        for field, invalid in [
            ("schema", True),
            ("profile", "another-profile"),
            ("executed", True),
            ("module_bytes", 124),
            ("admission_ns", -1),
            ("compiler_setup_ns", False),
        ]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_admission({**record, field: invalid}, 123)
        with self.assertRaises(ValueError):
            validate_admission([], 123)

    def test_ten_sample_percentiles(self):
        self.assertEqual(summary(list(range(10))), {"p50_ns": 4.5, "p95_ns": 9})

    def test_incomplete_measurements_are_not_a_gate_result(self):
        with self.assertRaises(ValueError):
            summary([1] * 9)

    def test_edit_changes_one_real_argument(self):
        self.assertEqual(
            edit_source("kernal_api_v1_bindings::clock_sleep(10)", 10, 11), "kernal_api_v1_bindings::clock_sleep(11)"
        )
        with self.assertRaises(ValueError):
            edit_source("no anchor", 10, 11)
        with self.assertRaises(ValueError):
            edit_source("kernal_api_v1_bindings::clock_sleep(10)" * 2, 10, 11)

    def test_failed_command_preserves_log_but_produces_no_sample(self):
        with tempfile.TemporaryDirectory(prefix="kernal-measure-test-") as directory:
            root = Path(directory)
            log = root / "failure.log"
            failure = subprocess.CompletedProcess(["unused"], 1, "output", "failure")
            with patch("measure_core.subprocess.run", return_value=failure):
                with self.assertRaises(RuntimeError):
                    run(["unused"], root, log)
            self.assertIn("failure", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
