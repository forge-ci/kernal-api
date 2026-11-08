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
        self.event = "pull_request"
        self.sibling: dict = {"id": 2, "head_sha": "abc", "run_attempt": 1}

        def fake_fetch(url: str, token: str) -> dict:
            self.calls.append(url)
            if url.endswith("/actions/runs/1"):
                return {"workflow_id": 7, "head_sha": "abc", "event": self.event}
            if "/workflows/7/runs" in url:
                return {"workflow_runs": [{"id": 1, "head_sha": "abc"}, self.sibling]}
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

    def test_derived_aggregate_failure_rides_on_a_cancellation(self) -> None:
        cancelled_run = [job("linux", "cancelled"), job("Full coverage", "failure")]
        self.assertTrue(ci_summary.only_cancellations(cancelled_run))
        self.assertFalse(ci_summary.only_cancellations([job("Full coverage", "failure")]))
        self.assertFalse(ci_summary.only_cancellations([job("Full coverage", "timed_out"), job("linux", "cancelled")]))
        self.assertFalse(ci_summary.only_cancellations([job("verify", "failure"), job("linux", "cancelled")]))

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

    def live(self, status: str) -> None:
        self.sibling = {"id": 2, "head_sha": "abc", "run_attempt": 1, "status": status, "conclusion": None}
        self.sibling_jobs = [job("Build (x)", None), job("CI summary", None)]

    def test_running_or_queued_sibling_supersedes_a_pull_request_run(self) -> None:
        for status in ("queued", "in_progress"):
            self.live(status)
            found = ci_summary.superseding_run("o/r", "1", "t")
            self.assertIsNotNone(found)
            self.assertIn("run 2", found or "")

    def test_live_sibling_does_not_supersede_merge_group_or_push(self) -> None:
        for event in ("merge_group", "push", "workflow_dispatch"):
            self.event = event
            self.live("in_progress")
            self.assertIsNone(ci_summary.superseding_run("o/r", "1", "t"))

    def test_green_completed_sibling_still_supersedes_merge_group(self) -> None:
        self.event = "merge_group"
        self.sibling = {"id": 2, "head_sha": "abc", "run_attempt": 1, "status": "completed", "conclusion": "success"}
        self.assertIsNotNone(ci_summary.superseding_run("o/r", "1", "t"))

    def test_completed_unsuccessful_sibling_never_counts(self) -> None:
        # Even with a green gate: a cancelled sibling's gate may have deferred to this run.
        for conclusion in ("failure", "cancelled", "timed_out"):
            self.sibling = {
                "id": 2,
                "head_sha": "abc",
                "run_attempt": 1,
                "status": "completed",
                "conclusion": conclusion,
            }
            self.assertIsNone(ci_summary.superseding_run("o/r", "1", "t"))

    def test_no_sibling_does_not_supersede(self) -> None:
        self.sibling = {"id": 1, "head_sha": "abc"}
        self.assertIsNone(ci_summary.superseding_run("o/r", "1", "t"))


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.consulted = False

    def run_main(self, own_jobs: list[dict], sibling: str | None) -> int:
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_TOKEN": "t"}
        with (
            unittest.mock.patch.dict("os.environ", env),
            unittest.mock.patch.object(ci_summary, "list_jobs", return_value=own_jobs),
            unittest.mock.patch.object(ci_summary, "superseding_run", return_value=sibling) as found,
            unittest.mock.patch("builtins.print"),
        ):
            code = ci_summary.main()
        self.consulted = found.called
        return code

    def test_cancelled_run_with_sibling_passes(self) -> None:
        self.assertEqual(self.run_main([job("linux", "cancelled")], "live (in_progress) run 2"), 0)

    def test_cancelled_run_without_sibling_fails(self) -> None:
        self.assertEqual(self.run_main([job("linux", "cancelled")], None), 1)

    def test_real_failure_mixed_with_cancellations_fails_without_consulting_siblings(self) -> None:
        self.assertEqual(self.run_main([job("linux", "cancelled"), job("verify", "failure")], "green 2"), 1)
        self.assertFalse(self.consulted)


if __name__ == "__main__":
    unittest.main()
