"""Contract for the cheap routine gate and exact-SHA full validation."""

import re
import unittest
from pathlib import Path

from ci import ci_mode, full_coverage

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"


class ModeTests(unittest.TestCase):
    def test_event_selection(self):
        sha = "a" * 40
        pr = {"pull_request": {"head": {"sha": sha}, "labels": []}}
        self.assertEqual(ci_mode.select("pull_request", pr, "b" * 40), ("minimal", sha))
        pr["pull_request"]["labels"] = [{"name": "ci-test"}]
        self.assertEqual(ci_mode.select("pull_request", pr, "b" * 40), ("test", sha))
        pr["pull_request"]["labels"].append({"name": "ci-full"})
        self.assertEqual(ci_mode.select("pull_request", pr, "b" * 40), ("full", sha))
        pr["pull_request"]["labels"] = []
        self.assertEqual(ci_mode.select("pull_request", pr, "b" * 40), ("minimal", sha))
        self.assertEqual(ci_mode.select("push", {}, sha), ("minimal", sha))
        self.assertEqual(ci_mode.select("merge_group", {}, sha), ("minimal", sha))
        self.assertEqual(
            ci_mode.select(
                "workflow_dispatch", {"inputs": {"candidate_sha": sha}}, "b" * 40
            ),
            ("full", sha),
        )

    def test_dispatch_requires_exact_sha(self):
        with self.assertRaisesRegex(ValueError, "40 hexadecimal"):
            ci_mode.select(
                "workflow_dispatch", {"inputs": {"candidate_sha": "main"}}, "a" * 40
            )

    def test_workflow_wires_modes_and_exact_checkout(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "github.event_name == 'workflow_dispatch' && inputs.candidate_sha || github.ref",
            text,
        )
        self.assertIn(
            "types: [opened, reopened, synchronize, ready_for_review, labeled, unlabeled, edited]",
            text,
        )
        self.assertIn("candidate_sha:", text)
        self.assertIn("merge_group:\n    types: [checks_requested]", text)
        # CACHE-016: main's Dylint cache save must not be cancelled by a later push.
        self.assertIn("cancel-in-progress: ${{ github.ref != 'refs/heads/main' }}", text)
        self.assertIn("python 3.12 ci/ci_mode.py", text)
        self.assertIn("mode: ${{ steps.mode.outputs.mode }}", text)
        self.assertIn(
            "ref: ${{ github.event_name == 'workflow_dispatch' && inputs.candidate_sha || github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}",
            text,
        )
        self.assertIn('ci/verify_source_sha.py "${{ steps.mode.outputs.sha }}"', text)
        for job in ("build", "test"):
            body = re.search(rf"(?ms)^  {job}:\n(.*?)(?=^  [\w-]+:|\Z)", text).group(1)
            self.assertIn("needs.linux.outputs.mode == 'full'", body)
            self.assertIn("ref: ${{ needs.linux.outputs.sha }}", body)
        # Dylint runs beside `linux`, so it selects its mode and SHA itself.
        dylints = re.search(r"(?ms)^  dylints:\n(.*?)(?=^  [\w-]+:|\Z)", text).group(1)
        self.assertNotIn("needs.linux", dylints)
        self.assertIn("python 3.12 ci/ci_mode.py", dylints)
        self.assertIn('ci/verify_source_sha.py "${{ steps.mode.outputs.sha }}"', dylints)
        self.assertIn("steps.mode.outputs.mode != 'minimal'", text)
        self.assertIn("steps.mode.outputs.mode == 'full'", text)
        self.assertIn("name: Full coverage", text)
        self.assertIn("always() && needs.linux.outputs.mode == 'full'", text)
        self.assertIn("ci/full_coverage.py", text)
        self.assertIn("name: Dylint workspace", text)
        self.assertNotIn("name: Dylint (${{ matrix.os }})", text)
        # The Dylint aggregate (#363) is the lint step itself, not a `needs:`
        # sentinel job that waits for a runner after Dylint finishes.
        self.assertNotIn("  dylint-coverage:", text)
        dylints = re.search(r"(?ms)^  dylints:\n(.*?)(?=^  [\w-]+:|\Z)", text).group(1)
        # Only a valid per-job attestation may suppress this job. The trust
        # policy forces extended/full labels to execute it remotely.
        self.assertEqual(
            re.findall(r"(?m)^    if: (.*)$", dylints),
            ["needs.verify.outputs.skip_dylints != 'true'"],
        )
        self.assertNotIn("matrix:", dylints)
        self.assertIn("run: python3 ci/dylint_steps.py lanes", dylints)
        lanes = (WORKFLOW.parents[2] / "ci/dylint_steps.py").read_text(encoding="utf-8")
        self.assertIn('raise gha.StepError(f"Dylint lanes failed: {\' \'.join(failed)}")', lanes)
        self.assertIn('workspace_log = logs / "workspace-dylint.log"', lanes)
        self.assertEqual(
            text.count('ci/verify_source_sha.py "${{ needs.linux.outputs.sha }}"'), 2
        )
        linux = re.search(r"(?ms)^  linux:\n(.*?)(?=^  [\w-]+:|\Z)", text).group(1)
        for name in (
            "Check every feature in isolation",
            "Run real semantic webview lifecycle and capture proofs",
            "Build the target-independent Wasm guests once",
            "Run the x86_64 Linux compiler-policy proofs",
            "Run the x86_64 Linux screenshot and containment proofs",
            "Run the full test suite",
        ):
            with self.subTest(step=name):
                step = linux.split(f"- name: {name}\n", 1)[1].split("\n      - ", 1)[0]
                self.assertIn("if: steps.mode.outputs.mode == 'full'", step)
        extra = linux.split(
            "- name: Test JSON with unified arbitrary-precision backend feature\n", 1
        )[1]
        self.assertIn(
            "if: steps.mode.outputs.mode != 'minimal'", extra.split("\n      - ", 1)[0]
        )


class FullCoverageTests(unittest.TestCase):
    def test_skipped_macos_execution_fails_closed(self):
        sha = "a" * 40
        jobs = [
            {"name": name, "conclusion": "success", "steps": []}
            for name in full_coverage.REQUIRED_JOBS
        ]
        needs = {name: {"result": "success"} for name in full_coverage.REQUIRED_NEEDS}
        self.assertTrue(
            any(
                "Run this host's prebuilt tests" in error
                for error in full_coverage.failures(sha, sha, needs, jobs)
            )
        )
        for job in jobs:
            if job["name"].startswith("Test ("):
                job["steps"] = [
                    {"name": name, "conclusion": "success"}
                    for name in full_coverage.REQUIRED_TEST_STEPS
                ]
        self.assertEqual(full_coverage.failures(sha, sha, needs, jobs), [])

    def test_required_legs_match_workflow_matrices(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        build = re.search(r"(?ms)^  build:\n(.*?)(?=^  [\w-]+:|\Z)", text).group(1)
        test = re.search(r"(?ms)^  test:\n(.*?)(?=^  [\w-]+:|\Z)", text).group(1)
        dylints = re.search(r"(?ms)^  dylints:\n(.*?)(?=^  [\w-]+:|\Z)", text).group(1)
        self.assertEqual(
            set(re.findall(r"^            target: (\S+)$", build, re.MULTILINE)),
            set(full_coverage.TARGETS),
        )
        self.assertEqual(
            set(re.findall(r"^            target: (\S+)$", test, re.MULTILINE)),
            set(full_coverage.TARGETS),
        )
        self.assertIn("runs-on: ubuntu-24.04", dylints)
        self.assertNotIn("matrix:", dylints)

    def test_all_required_legs_and_sha(self):
        sha = "a" * 40
        jobs = [
            {
                "name": name,
                "conclusion": "success",
                "steps": (
                    [
                        {"name": step, "conclusion": "success"}
                        for step in full_coverage.REQUIRED_TEST_STEPS
                    ]
                    if name.startswith("Test (")
                    else []
                ),
            }
            for name in full_coverage.REQUIRED_JOBS
        ]
        needs = {
            name: {"result": "success"}
            for name in ("linux", "build", "test", "dylints")
        }
        self.assertEqual(full_coverage.failures(sha, sha, needs, jobs), [])
        self.assertIn(
            "source SHA", full_coverage.failures(sha, "b" * 40, needs, jobs)[0]
        )
        jobs.pop()
        self.assertTrue(
            any(
                "missing" in error
                for error in full_coverage.failures(sha, sha, needs, jobs)
            )
        )
        jobs[0]["conclusion"] = "skipped"
        self.assertTrue(
            any(
                "skipped" in error
                for error in full_coverage.failures(sha, sha, needs, jobs)
            )
        )
        needs["test"]["result"] = "failure"
        self.assertIn("test: failure", full_coverage.failures(sha, sha, needs, jobs))


if __name__ == "__main__":
    unittest.main()
