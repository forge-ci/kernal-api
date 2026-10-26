"""Tests for the workflow-step scripts that replaced inline shell (GEN-005)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci import build_steps, dylint_steps, gha, host_steps, release_steps

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CONTROL_TOKENS = ("&&", "||", ";", "|", "$(", "`", "<<")


def temporary_directory(case: unittest.TestCase) -> Path:
    path = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, path)
    return path


class ShellBudgetTests(unittest.TestCase):
    """Every `run:` is one line calling one command: logic lives in ci/*.py."""

    def run_blocks(self, text: str) -> list[str]:
        blocks = []
        lines = text.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"^(\s*)(?:- )?run: ?(.*)$", line)
            if not match:
                continue
            indent, value = len(match.group(1)), match.group(2)
            if value in ("|", ">-", ">", "|-"):
                body = []
                for following in lines[index + 1 :]:
                    if following.strip() and len(following) - len(following.lstrip()) <= indent:
                        break
                    body.append(following.strip())
                value = (" " if value.startswith(">") else "\n").join(part for part in body if part)
            blocks.append(value)
        return blocks

    def test_no_multi_line_or_control_syntax_run_steps(self):
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            text = workflow.read_text(encoding="utf-8")
            with self.subTest(workflow=workflow.name):
                self.assertNotRegex(text, r"(?m)^\s+shell: (pwsh|powershell|cmd)\s*$")
                for block in self.run_blocks(text):
                    self.assertNotIn("\n", block, block)
                    for token in CONTROL_TOKENS:
                        self.assertNotIn(token, block, block)

    def test_no_tracked_shell_scripts_but_the_recovery_guest(self):
        listed = gha.read(["git", "ls-files", "*.sh", "*.ps1", "*.bat", "*.cmd"], cwd=ROOT)
        self.assertEqual(listed.returncode, 0)
        scripts = sorted(line for line in listed.lines if line)
        self.assertEqual(scripts, ["ci/macos-x64/recovery-guest.sh"])


class VerifySourceShaTests(unittest.TestCase):
    def run_script(self, expected: str) -> int:
        return subprocess.run(
            [sys.executable, str(ROOT / "ci/verify_source_sha.py"), expected],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            check=False,
        ).returncode

    def test_matches_head_only(self):
        head = gha.stdout_of(["git", "rev-parse", "HEAD"], cwd=ROOT)
        self.assertEqual(self.run_script(head), 0)
        self.assertNotEqual(self.run_script("0" * 40), 0)


class DylintDecisionTests(unittest.TestCase):
    def test_lint_surfaces(self):
        for path, surface in (
            ("dylints/kernal_api_boundary/src/lib.rs", True),
            ("ci/dylint-shim/cargo", True),
            (".github/workflows/ci.yml", True),
            (".github/workflows/release.yml", False),
            ("src/lib.rs", False),
        ):
            with self.subTest(path=path):
                self.assertEqual(bool(dylint_steps.LINT_SURFACES.match(path)), surface)

    def test_missing_base_runs_the_suites(self):
        self.assertFalse(dylint_steps.lint_libraries_unchanged(""))

    def test_workspace_pass_always_runs_and_suites_only_on_request(self):
        env = {"DYLINT_TOOLCHAIN": "nightly-test"}
        with patch.dict(os.environ, env | {"RUN_LINT_LIBRARY_TESTS": "false"}):
            self.assertEqual([lane.name for lane in dylint_steps.lanes_to_run()], ["workspace-dylint"])
        with patch.dict(os.environ, env | {"RUN_LINT_LIBRARY_TESTS": "true"}):
            lanes = dylint_steps.lanes_to_run()
        self.assertEqual(
            [lane.name for lane in lanes], ["facade-boundary-tests", "platform-boundary-tests", "workspace-dylint"]
        )
        workspace = lanes[-1].command
        self.assertEqual(workspace[:5], ("soldr", "cargo", "dylint", "--all", "--workspace"))
        self.assertNotIn("--target", workspace)
        for lane in lanes[:2]:
            self.assertTrue(lane.cwd and lane.cwd.startswith("dylints/"))


class BuildStepTests(unittest.TestCase):
    def test_archive_config_ships_every_pdb(self):
        root = temporary_directory(self)
        deps = root / "target/x86_64-pc-windows-msvc/debug/deps"
        deps.mkdir(parents=True)
        (deps / "kernal_symbolize-abc.pdb").touch()
        (root / ".config").mkdir()
        (root / ".config/nextest.toml").write_text("[profile.default]\n", encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, cwd)
        with patch.dict(os.environ, {"RUNNER_TEMP": str(root)}):
            config = build_steps.archive_config("x86_64-pc-windows-msvc")
        self.assertEqual(
            config.read_text(encoding="utf-8"),
            "[profile.default]\n\n[profile.default.archive]\ninclude = [\n"
            '  { path = "x86_64-pc-windows-msvc/debug/deps/kernal_symbolize-abc.pdb", '
            'relative-to = "target", on-missing = "error" },\n]\n',
        )

    def test_archive_config_requires_a_pdb(self):
        root = temporary_directory(self)
        cwd = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, cwd)
        with self.assertRaisesRegex(gha.StepError, "produced no PDB"):
            build_steps.archive_config("aarch64-pc-windows-msvc")

    def test_native_lanes_never_name_the_triple(self):
        with patch.object(build_steps.gha, "run") as run, patch.dict(os.environ, {"KERNAL_TARGET_FEATURES": "a,b"}):
            build_steps.archive("aarch64-unknown-linux-gnu", "")
        (command,), _ = run.call_args
        self.assertNotIn("--target", command)
        self.assertEqual(command[:4], ["soldr", "cargo", "nextest", "archive"])

    def test_newest_llvm_rc_wins(self):
        paths = ["/usr/lib/llvm-9/bin/llvm-rc", "/usr/lib/llvm-18/bin/llvm-rc", "/usr/lib/llvm-14/bin/llvm-rc"]
        self.assertEqual(max(paths, key=build_steps.version_key), "/usr/lib/llvm-18/bin/llvm-rc")


class HostStepTests(unittest.TestCase):
    def test_a_host_must_run_its_own_platform_tree(self):
        root = temporary_directory(self)
        cwd = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, cwd)
        Path("nextest-output.txt").write_text("PASS platform_macos::tests::x\n", encoding="utf-8")
        with patch.dict(os.environ, {"RUNNER_OS": "macOS"}):
            self.assertEqual(host_steps.prove_own_code(), 0)
        with (
            patch.dict(os.environ, {"RUNNER_OS": "Windows"}),
            self.assertRaisesRegex(gha.StepError, "did not execute its own code"),
        ):
            host_steps.prove_own_code()


class ReleaseStepTests(unittest.TestCase):
    def test_published_digests(self):
        release = {"urls": [{"filename": "a.whl", "digests": {"sha256": "ff"}}, {"filename": "b"}]}
        self.assertEqual(release_steps.published_digests(release), {("a.whl", "ff")})
        self.assertEqual(release_steps.published_digests(None), set())

    def test_version_must_match_every_manifest(self):
        root = temporary_directory(self)
        (root / "python/kernal_api").mkdir(parents=True)
        (root / "Cargo.toml").write_text('[package]\nversion = "1.2.3"\n', encoding="utf-8")
        (root / "pyproject.toml").write_text('version = "1.2.3"\n', encoding="utf-8")
        (root / "python/kernal_api/__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(release_steps.verify_version("v1.2.3"), 0)
        with self.assertRaises(gha.StepError):
            release_steps.verify_version("v1.2.4")
        (root / "pyproject.toml").write_text('version = "1.2.2"\n', encoding="utf-8")
        with self.assertRaises(gha.StepError):
            release_steps.verify_version("v1.2.3")


class GithubFileTests(unittest.TestCase):
    def test_target_features_exports_to_github_env(self):
        root = temporary_directory(self)
        env_file = root / "env"
        subprocess.run(
            [sys.executable, str(ROOT / "ci/target_features.py"), "--github-env"],
            env=dict(os.environ, GITHUB_ENV=str(env_file)),
            stdout=subprocess.DEVNULL,
            check=True,
        )
        line = env_file.read_text(encoding="utf-8").strip()
        self.assertTrue(line.startswith("KERNAL_TARGET_FEATURES="), line)
        self.assertNotIn("build-resources", line)

    def test_guest_script_travels_as_one_delimited_output(self):
        root = temporary_directory(self)
        output = root / "output"
        subprocess.run(
            [sys.executable, str(ROOT / "ci/macos-x64/guest_script_output.py")],
            env=dict(os.environ, GITHUB_OUTPUT=str(output)),
            check=True,
        )
        text = output.read_text(encoding="utf-8")
        guest = (ROOT / "ci/macos-x64/recovery-guest.sh").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("script<<KERNAL_GUEST_SCRIPT_EOF\n"))
        self.assertTrue(text.endswith("\nKERNAL_GUEST_SCRIPT_EOF\n"))
        self.assertIn(guest.rstrip("\n"), text)


if __name__ == "__main__":
    unittest.main()
