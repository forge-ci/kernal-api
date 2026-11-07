"""The CI summary gate blocks on every unsuccessful conclusion except its exemptions."""

import unittest
import unittest.mock

from ci import ci_summary


def job(name: str, conclusion: str | None) -> dict:
    return {"name": name, "conclusion": conclusion}


class CiSummaryTests(unittest.TestCase):
    def test_every_unsuccessful_conclusion_blocks(self) -> None:
        for conclusion in ("failure", "cancelled", "timed_out", "startup_failure", "action_required"):
            self.assertEqual(ci_summary.blocking_jobs([job("linux", conclusion)]), [job("linux", conclusion)])

    def test_success_and_skips_pass(self) -> None:
        jobs = [job("verify", "success"), job("linux", "skipped"), job("Build (x)", "neutral")]
        self.assertEqual(ci_summary.blocking_jobs(jobs), [])

    def test_only_the_gate_itself_is_exempt(self) -> None:
        self.assertEqual(ci_summary.blocking_jobs([job("CI summary", None)]), [])
        self.assertEqual(
            ci_summary.blocking_jobs([job("Dylint workspace", "cancelled")]), [job("Dylint workspace", "cancelled")]
        )

    def test_failed_maintenance_job_blocks_the_gate(self) -> None:
        for name in ("Cache janitor", "Retire superseded and disabled cache generations"):
            self.assertEqual(ci_summary.blocking_jobs([job(name, "failure")]), [job(name, "failure")])

    def test_skipped_maintenance_jobs_pass(self) -> None:
        jobs = [job("Cache janitor", "skipped"), job("Retire superseded and disabled cache generations", "skipped")]
        self.assertEqual(ci_summary.blocking_jobs(jobs), [])


class SupersededRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        self.sibling_jobs = [job("Build (x)", "success"), job("CI summary", "success")]

        def fake_fetch(url: str, token: str) -> dict:
            self.calls.append(url)
            if url.endswith("/actions/runs/1"):
                return {"workflow_id": 7, "head_sha": "abc"}
            if "/workflows/7/runs" in url:
                return {"workflow_runs": [{"id": 1, "head_sha": "abc"}, {"id": 2, "head_sha": "abc", "run_attempt": 1}]}
            return {"total_count": len(self.sibling_jobs), "jobs": self.sibling_jobs}

        self.patch = unittest.mock.patch.object(ci_summary, "fetch", fake_fetch)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()

    def test_only_pure_cancellations_qualify(self) -> None:
        self.assertTrue(ci_summary.only_cancellations([job("a", "cancelled")]))
        self.assertFalse(ci_summary.only_cancellations([]))
        for other in ("failure", "timed_out", "startup_failure", "action_required"):
            self.assertFalse(ci_summary.only_cancellations([job("a", "cancelled"), job("b", other)]))

    def test_green_sibling_supersedes(self) -> None:
        self.assertIsNotNone(ci_summary.superseding_run("o/r", "1", "t"))
        self.assertIn("/runs/2/attempts/1/jobs", self.calls[-1])

    def test_red_or_pending_sibling_does_not(self) -> None:
        for conclusion in ("failure", "cancelled", None):
            self.sibling_jobs = [job("CI summary", conclusion)]
            self.assertIsNone(ci_summary.superseding_run("o/r", "1", "t"))

    def test_own_run_never_counts(self) -> None:
        self.sibling_jobs = []
        self.assertIsNone(ci_summary.superseding_run("o/r", "1", "t"))


if __name__ == "__main__":
    unittest.main()
