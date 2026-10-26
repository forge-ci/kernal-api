"""Fail-closed release candidate proof checks."""

import unittest

from ci.release_ci_gate import failures


class ReleaseCIGateTests(unittest.TestCase):
    def test_only_successful_full_dispatch_on_exact_sha_passes(self):
        sha = "a" * 40
        run = {
            "id": 123,
            "head_sha": sha,
            "event": "workflow_dispatch",
            "path": "zackees/kernal-api/.github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
        }
        jobs = [{"name": "Full coverage", "conclusion": "success"}]
        self.assertEqual(failures(run, jobs, sha, "123"), [])
        for change in (
            {"head_sha": "b" * 40},
            {"event": "push"},
            {"status": "in_progress"},
            {"conclusion": "failure"},
        ):
            with self.subTest(change=change):
                self.assertTrue(failures(run | change, jobs, sha, "123"))
        self.assertTrue(failures(run, [], sha, "123"))
        self.assertTrue(failures(run, [{"name": "Full coverage", "conclusion": "skipped"}], sha, "123"))


if __name__ == "__main__":
    unittest.main()
