"""Hostile Bosn reports cannot replace executed minimal workflow checks."""

import copy
import unittest
from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.workflow_scan import as_dict, jobs_of, load, steps_of

from ci.replay_evidence import LINUX, VERIFY, verify_checks


def report(job: str) -> dict[str, JsonValue]:
    """Construct a JSON boundary matching the original runner report shape."""
    checks = LINUX
    return {
        "schema_version": 1,
        "provider": "github",
        "repository": "zackees/kernal-api",
        "engine": "act",
        "workspace": "/repo",
        "sha": "a" * 40,
        "git_tree": "b" * 40,
        "dirty": None,
        "tree_digest": "c" * 64,
        "workflow": ".github/workflows/ci.yml",
        "job": job,
        "trigger": "pr",
        "event": "pull_request",
        "mode": "minimal",
        "state": "done",
        "conclusion": "success",
        "exit_code": 0,
        "act_exit_code": 0,
        "cleanup": "removed",
        "act_version": "0.2.89-act2.4",
        "tree": {
            "malformed_lines": 0,
            "groups": [
                {
                    "jobs": [
                        {
                            "key": expected.key,
                            "job_id": expected.identity[-1].job_id,
                            "matrix": {},
                            "identity": [{"jobID": part.job_id, "matrix": None} for part in expected.identity],
                            "status": "completed",
                            "conclusion": "success",
                            "sections": [
                                {
                                    "name": name,
                                    "stage": "Main",
                                    "status": "completed",
                                    "conclusion": "success",
                                    "first_seq": 1,
                                    "last_seq": 2,
                                }
                                for name in expected.steps
                            ],
                        }
                        for expected in (VERIFY, checks)
                    ]
                }
            ],
        },
    }


class ExecutedChecksTests(unittest.TestCase):
    def verify(self, raw, job="linux"):
        verify_checks(raw, Path("/repo"), "a" * 40, "b" * 40, job)

    def test_each_selected_job_requires_its_verifier_and_checks(self):
        self.verify(report("linux"), "linux")

    def test_dylints_is_never_a_bosn_replay(self):
        """Lint never runs under bosn; soldr lint only (host `lint` lane)."""
        with self.assertRaises(ValueError):
            self.verify(report("dylints"), "dylints")

    def test_green_aggregate_does_not_excuse_any_skipped_check(self):
        for job in ("linux",):
            raw = report(job)
            for actual in raw["tree"]["groups"][0]["jobs"]:
                for section in actual["sections"]:
                    with self.subTest(job=job, check=section["name"]):
                        section["conclusion"] = "skipped"
                        with self.assertRaises(ValueError):
                            self.verify(raw, job)
                        section["conclusion"] = "success"

    def test_desktop_status_check_cannot_be_missing_or_only_a_post_step(self):
        raw = report("linux")
        sections = raw["tree"]["groups"][0]["jobs"][1]["sections"]
        section = sections.pop()
        with self.assertRaises(ValueError):
            self.verify(raw)
        section["stage"] = "Post"
        sections.append(section)
        with self.assertRaises(ValueError):
            self.verify(raw)

    def test_duplicate_or_unexecuted_check_is_rejected(self):
        raw = report("linux")
        sections = raw["tree"]["groups"][0]["jobs"][1]["sections"]
        sections.append(copy.deepcopy(sections[-1]))
        with self.assertRaises(ValueError):
            self.verify(raw)
        sections.pop()
        sections[-1]["first_seq"] = None
        with self.assertRaises(ValueError):
            self.verify(raw)

    def test_either_repository_name_is_this_repository(self):
        for name in ("zackees/kernal-api", "forge-ci/kernal-api"):
            with self.subTest(name=name):
                self.verify({**report("linux"), "repository": name})
        with self.assertRaises(ValueError):
            self.verify({**report("linux"), "repository": "other/kernal-api"})

    def test_wrong_selection_cannot_borrow_another_job(self):
        with self.assertRaises(ValueError):
            self.verify(report("linux"), "dylints")
        with self.assertRaises(ValueError):
            self.verify(report("linux"), "full")

    def test_workflow_minimal_checks_cannot_drift_from_the_proof(self):
        root = Path(__file__).resolve().parents[1]
        parsed = load(root, root / ".github/workflows/ci.yml")
        self.assertIsNotNone(parsed.document)
        jobs = jobs_of(as_dict(parsed.document))
        expected_jobs = {"verify": VERIFY, "linux": LINUX}
        excluded = {
            "steps.mode.outputs.mode == 'full'",
            "steps.mode.outputs.mode != 'minimal'",
            "always() && steps.mode.outputs.mode == 'full'",
            "failure() && github.event_name == 'pull_request' && steps.mode.outputs.mode != 'full'",
        }
        for job_id, expected in expected_jobs.items():
            required = []
            for step in steps_of(jobs[job_id]):
                if "run" not in step and "uses" not in step:
                    continue
                condition = step.get("if")
                if condition in excluded:
                    continue
                self.assertIn(condition, (None, "steps.mode.outputs.mode != 'full'"))
                required.append(step.get("name"))
            with self.subTest(job=job_id):
                self.assertEqual(tuple(required), expected.steps)

    def test_bosn_replayed_linux_job_holds_no_lint_step(self):
        """Lint never runs under bosn; soldr lint only.

        `linux` is the job the bosn lane replays, so no step in it may format,
        Clippy, Dylint or Ruff the tree; those run in `dylints`, which the host
        `lint` lane (`soldr lint`) attests.
        """
        root = Path(__file__).resolve().parents[1]
        parsed = load(root, root / ".github/workflows/ci.yml")
        self.assertIsNotNone(parsed.document)
        jobs = jobs_of(as_dict(parsed.document))
        for step in steps_of(jobs["linux"]):
            run = str(step.get("run", ""))
            for word in ("cargo fmt", "cargo clippy", "dylint", "ruff", "soldr lint"):
                with self.subTest(step=step.get("name"), word=word):
                    self.assertNotIn(word, run)


if __name__ == "__main__":
    unittest.main()
