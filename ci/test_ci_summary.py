"""The CI summary gate blocks on every unsuccessful conclusion except its exemptions."""

import unittest

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

    def test_only_the_gate_and_maintenance_jobs_are_exempt(self) -> None:
        jobs = [
            job("CI summary", None),
            job("Cache janitor", "failure"),
            job("Retire superseded and disabled cache generations", "failure"),
        ]
        self.assertEqual(ci_summary.blocking_jobs(jobs), [])
        self.assertEqual(
            ci_summary.blocking_jobs([job("Dylint workspace", "cancelled")]), [job("Dylint workspace", "cancelled")]
        )


if __name__ == "__main__":
    unittest.main()
