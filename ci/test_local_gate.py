"""The local gate accepts only completed, source-bound act2 evidence."""

import subprocess
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import sys

from ci.local_gate import (
    LINT_COMMAND,
    CommandFailed,
    NativeHost,
    RunProof,
    main,
    output,
    verify_native_host,
    verify_pr_base,
    verify_run,
)


class FastPreflightTests(unittest.TestCase):
    def test_failing_guards_never_submit_an_engine_run(self):
        with (
            patch("sys.argv", ["local_gate.py"]),
            patch(
                "ci.local_gate.output",
                side_effect=[
                    "refs/remotes/origin/main",
                    "",
                    "linux/x86_64",
                    "a" * 40,
                ],
            ),
            patch(
                "ci.local_gate.native_host",
                return_value=NativeHost("Linux", "x86_64", "linux/x86_64"),
            ),
            patch("ci.local_gate.document", return_value={"run": "stub"}) as submit,
            patch(
                "ci.local_gate.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, ["guards"]),
            ),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                main()
            submit.assert_not_called()


class LintLaneTests(unittest.TestCase):
    def test_lint_lane_execs_the_full_soldr_lint_without_an_engine(self):
        with (
            patch("sys.argv", ["local_gate.py", "--job", "lint"]),
            patch("ci.local_gate.os.chdir"),
            patch("ci.local_gate.os.execvp", side_effect=SystemExit(0)) as execvp,
            patch("ci.local_gate.output") as engine,
        ):
            with self.assertRaises(SystemExit):
                main()
            execvp.assert_called_once_with("uv", list(LINT_COMMAND))
            self.assertEqual(LINT_COMMAND[-1], "ci/lint_lane.py")
            engine.assert_not_called()


class PrBaseTests(unittest.TestCase):
    def test_main_base_passes(self):
        verify_pr_base("refs/remotes/origin/main")

    def test_feature_or_unknown_base_is_rejected(self):
        for ref in ("refs/remotes/origin/feature", "", "refs/heads/main"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                verify_pr_base(ref)


class NativeHostTests(unittest.TestCase):
    def test_linux_x64_host_and_daemon_pass(self):
        verify_native_host(NativeHost("Linux", "x86_64", "linux/x86_64"))
        verify_native_host(NativeHost("Linux", "amd64", "linux/amd64"))

    def test_arm_emulation_remote_arm_and_other_os_fail(self):
        for host in (
            NativeHost("Linux", "aarch64", "linux/x86_64"),
            NativeHost("Linux", "x86_64", "linux/aarch64"),
            NativeHost("Darwin", "x86_64", "linux/x86_64"),
            NativeHost("Linux", "x86_64", "unknown"),
        ):
            with self.subTest(host=host), self.assertRaises(ValueError):
                verify_native_host(host)


class RunProofTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path("/repo").resolve()
        self.sha = "a" * 40
        self.git_tree = "b" * 40
        self.proof = RunProof(
            self.workspace,
            self.sha,
            None,
            "act",
            "0.2.89-act2.1",
            ".github/workflows/ci.yml",
            "linux",
            "minimal",
            "done",
            "success",
            0,
            1,
            1,
            0,
            self.git_tree,
            "pr",
            "pull_request",
            "removed",
            "c" * 64,
        )

    def test_completed_matching_run_passes(self):
        verify_run(self.proof, self.workspace, self.sha, self.git_tree)

    def test_completed_dylint_run_proves_only_dylint(self):
        proof = replace(self.proof, job="dylints")
        verify_run(proof, self.workspace, self.sha, self.git_tree, job="dylints")
        with self.assertRaises(ValueError):
            verify_run(proof, self.workspace, self.sha, self.git_tree)

    def test_incomplete_wrong_tree_and_upstream_act_fail(self):
        for changes in (
            {"workspace": Path("/another")},
            {"sha": "b" * 40},
            {"dirty": True},
            {"state": "running"},
            {"conclusion": "failure"},
            {"act_version": "0.2.89"},
            {"engine": "host"},
            {"workflow": "other.yml"},
            {"job": "test"},
            {"mode": "full"},
            {"exit_code": 1},
            {"total": 0},
            {"completed": 0},
            {"failed": 1},
            {"git_tree": "d" * 40},
            {"trigger": "workflow_dispatch"},
            {"event": "push"},
            {"cleanup": "failed"},
            {"cleanup": "pending"},
            {"payload_sha256": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verify_run(replace(self.proof, **changes), self.workspace, self.sha, self.git_tree)

    def test_missing_boundary_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            RunProof.from_json({})

    def test_original_boundary_preserves_source_and_cleanup(self):
        raw = asdict(self.proof)
        raw.update(
            workspace=str(self.workspace),
            jobs={"total": 1, "completed": 1, "failed": 0},
            schema_version=1,
            provider="github",
            repository="zackees/kernal-api",
            act_exit_code=0,
            tree_digest="d" * 64,
        )
        self.assertEqual(RunProof.from_json(raw), self.proof)
        # A checkout cloned from the moved repository is the same repository (#436).
        self.assertEqual(RunProof.from_json({**raw, "repository": "forge-ci/kernal-api"}), self.proof)
        for field in ("git_tree", "trigger", "event", "cleanup", "payload_sha256"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                RunProof.from_json({key: value for key, value in raw.items() if key != field})
        for field, value in {
            "schema_version": True,
            "provider": "other",
            "repository": "other/repo",
            "act_exit_code": 1,
            "tree_digest": "",
        }.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                RunProof.from_json({**raw, field: value})


if __name__ == "__main__":
    unittest.main()


class OutputTests(unittest.TestCase):
    def test_failure_reports_the_command_s_stderr_and_exit_code(self):
        script = "import sys; print('daemon is bosn 0.1.15, client 0.1.13', file=sys.stderr); sys.exit(3)"
        with patch("sys.stderr"), self.assertRaises(CommandFailed) as caught:
            output([sys.executable, "-c", script])
        self.assertIn("daemon is bosn 0.1.15, client 0.1.13", str(caught.exception))
        self.assertIn("exited 3", str(caught.exception))
        self.assertIn("BOSN_GATE_STATE_DIR", str(caught.exception))

    def test_success_returns_stdout_only(self):
        script = "import sys; print('noise', file=sys.stderr); print('{}')"
        with patch("sys.stderr"):
            self.assertEqual(output([sys.executable, "-c", script]).strip(), "{}")
