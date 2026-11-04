"""Guard the Intel macOS lane against the failure class it just came out of.

`macos-x64-tests.yml` spent its entire life red because it depended on a guest
image published out of band into this repository's own GHCR namespace, by a
workflow that was never committed. These tests fail if that shape — a
repository-scoped guest image, a hand-run bake step, or a reference to a file
that does not exist — ever returns, and if the coverage assertion that makes the
lane meaningful is weakened.
"""

from __future__ import annotations

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/macos-x64-tests.yml"
GUEST_DIR = ROOT / "ci/macos-x64"
GUEST_SCRIPT = GUEST_DIR / "recovery-guest.sh"
VERIFY_SCRIPT = GUEST_DIR / "verify-guest-results.py"

# The four source-inspection policy tests read this crate's own tree through a
# compile-time CARGO_MANIFEST_DIR, which points at the Linux builder in the
# guest. The aarch64 lane excludes the same four.
# The four source-reading tests are one linked binary now (AGENTS.md:
# one category target, not one per file).
POLICY_BINARIES = ("source_policy",)


def code_only(text: str) -> str:
    """Drop whole-line comments.

    The workflow and the guest script both *explain* the failure this lane came
    from, and those explanations name the removed scripts. Only executable text
    may be checked for reintroducing them.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def load_verify_module():
    """Import verify-guest-results.py, whose filename is not an identifier."""
    spec = importlib.util.spec_from_file_location("kernal_verify_guest_results", VERIFY_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuestWorkflowShapeTests(unittest.TestCase):
    def workflow_text(self) -> str:
        return WORKFLOW.read_text(encoding="utf-8")

    def test_no_repository_scoped_guest_image_or_ghcr_login(self):
        text = self.workflow_text()
        self.assertNotIn("docker/login-action", text)
        self.assertNotIn("docker pull", text)
        self.assertNotRegex(text, r"ghcr\.io\/\$\{\{\s*github\.repository")
        # No guest image env var of any kind: the image is the action's
        # business, and naming one here is how the hand-baked dependency began.
        self.assertNotIn("GUEST_IMAGE", text)
        self.assertNotIn("docker run", text)

    def test_the_dead_guest_package_never_returns_as_an_image(self):
        # The broken package specifically, in case it comes back as a
        # "restore the old path" convenience. Matched as an image reference so
        # the diagnostics artifact name is not a false positive.
        self.assertNotRegex(self.workflow_text(), r"ghcr\.io/\S*macos-x64-guest")

    def test_uses_a_commit_pinned_recovery_action(self):
        text = self.workflow_text()
        matches = re.findall(r"uses:\s*(zackees/docker-mac-x64@(\S+))", text)
        self.assertEqual(len(matches), 1, "the lane must use exactly one guest action")
        ref = matches[0][1].split("#")[0].strip()
        self.assertRegex(ref, r"^[0-9a-f]{40}$", "the guest action must be commit-pinned")

    def test_installs_uv_before_using_it(self):
        """`uv` is not on a stock runner; every other lane installs it first.

        Without this the lane would pass everything expensive and then fail at
        the verdict step, half an hour in.
        """
        text = self.workflow_text()
        if "uv run" not in text:
            self.skipTest("this lane does not use uv")
        self.assertIn("astral-sh/setup-uv@", text)
        self.assertLess(
            text.index("astral-sh/setup-uv@"),
            text.index("uv run"),
            "setup-uv must precede the first uv invocation",
        )

    def test_lane_stays_advisory(self):
        text = self.workflow_text()
        triggers = text.split("on:", 1)[1].split("permissions:", 1)[0]
        self.assertIn("schedule:", triggers)
        self.assertIn("workflow_dispatch:", triggers)
        for forbidden in ("pull_request", "push:"):
            self.assertNotIn(forbidden, triggers)

    def test_no_reference_to_the_deleted_bake_path(self):
        """Every file the workflow names must exist -- no dangling references."""
        text = self.workflow_text()
        referenced = set(re.findall(r"ci/macos-x64/[A-Za-z0-9._-]+", text))
        self.assertTrue(referenced, "the workflow should run the lane's own scripts")
        for relative in referenced:
            with self.subTest(referenced=relative):
                self.assertTrue(
                    (ROOT / relative).is_file(),
                    f"{relative} is referenced by the workflow but does not exist",
                )
        code = code_only(text)
        for removed in ("bake.sh", "guest.sh", "Dockerfile.guest", "run-in-guest.sh"):
            with self.subTest(removed=removed):
                self.assertFalse((GUEST_DIR / removed).exists())
                # Comments may narrate the removed path; executed steps may not
                # invoke it. Anchored to a path boundary so the surviving
                # `recovery-guest.sh` is not reported as `guest.sh`.
                self.assertNotRegex(code, rf"(^|/){re.escape(removed)}\b")

    def test_readme_describes_only_the_current_path(self):
        """No document may describe a bootstrap that no longer exists."""
        readme = (GUEST_DIR / "README.md").read_text(encoding="utf-8")
        # The manual install steps that the deleted bake path required.
        for stale in ("Reinstall macOS", "Disk Utility", "ipconfig getifaddr"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, readme)
        # No command may invoke a script this change deleted.
        for removed in ("bake.sh", "Dockerfile.guest", "run-in-guest.sh", "guest.sh"):
            with self.subTest(removed=removed):
                self.assertNotIn(f"./{removed}", readme)


class GuestScriptTests(unittest.TestCase):
    def test_excludes_the_source_inspection_policy_binaries(self):
        text = GUEST_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("not binary(source_policy)", text)
        for binary in POLICY_BINARIES:
            with self.subTest(binary=binary):
                self.assertIn(binary, text)
        # The per-file names are gone; a filter naming one would silently match
        # nothing and run the tests it means to skip.
        for retired in ("daemon_frame_v1", "daemon_identity", "version_policy", "facade_policy"):
            with self.subTest(retired=retired):
                self.assertNotIn(f"binary({retired})", text)

    def test_provides_the_workspace_root_nextest_requires(self):
        """Replaying an archive needs a Cargo.toml at the remap root.

        Without it nextest exits 96 before running a single test, and the
        failure reads as a nextest setup error rather than a missing stub.
        """
        text = GUEST_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("Cargo.toml", text)
        self.assertRegex(text, r'--workspace-remap "\$WORKSPACE"')
        # The scratch directory has no manifest, so it cannot be the root.
        self.assertNotRegex(text, r'--workspace-remap "\$WORK"')

    def test_never_exits_non_zero_so_evidence_always_returns(self):
        """A guest boot costs minutes; a failure must still report."""
        text = GUEST_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("set -e", text)
        self.assertNotRegex(text, r"^\s*exit [1-9]", "the guest script must not exit non-zero")

    def exclusion_groups(self) -> dict[str, list[str]]:
        """Parse the EXCLUDE_* groups the guest script declares."""
        text = GUEST_SCRIPT.read_text(encoding="utf-8")
        groups = {}
        for match in re.finditer(r"^(EXCLUDE_[A-Z_]+)='([^']*)'", text, re.MULTILINE):
            groups[match.group(1)] = match.group(2).split()
        return groups

    def test_every_exclusion_group_is_present_and_non_empty(self):
        """A group emptied out would silently widen what the lane skips."""
        groups = self.exclusion_groups()
        expected = {
            "EXCLUDE_CAP_PRIMITIVES",
            "EXCLUDE_MACOS_RENAME",
        }
        self.assertEqual(set(groups), expected, "exclusion groups changed")
        for name, entries in groups.items():
            with self.subTest(group=name):
                self.assertTrue(entries, f"{name} is empty")
                for entry in entries:
                    self.assertRegex(entry, r"^[a-z0-9_]+$", f"{name}: {entry}")

    def test_exclusions_are_named_and_unique(self):
        groups = self.exclusion_groups()
        names = [n for entries in groups.values() for n in entries]
        self.assertEqual(len(names), len(set(names)), "duplicate exclusion entries")
        self.assertEqual(len(names), 15, "the documented exclusion count changed")

    def test_every_exclusion_is_applied_to_the_guest_filter(self):
        """Declaring a group but not using it would silently re-enable tests."""
        text = GUEST_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('EXCLUDED_TESTS="$EXCLUDE_CAP_PRIMITIVES', text)
        for group in self.exclusion_groups():
            with self.subTest(group=group):
                self.assertIn(f"${group}", text.split("EXCLUDED_TESTS=", 1)[1][:400])
        self.assertIn("test(~$name)", text)

    def test_is_bash_3_compatible(self):
        """Recovery ships bash 3.2: no associative arrays, no ${v^^}."""
        text = code_only(GUEST_SCRIPT.read_text(encoding="utf-8"))
        self.assertNotIn("declare -A", text)
        self.assertNotRegex(text, r"\$\{[A-Za-z_]+\^\^")
        self.assertNotIn("local -n", text)


class StageGuestShareTests(unittest.TestCase):
    """The share payload is the guest's only input; a bad URL is a silent 404."""

    def setUp(self):
        self.text = (GUEST_DIR / "stage_guest_share.py").read_text(encoding="utf-8")

    def test_checksum_url_is_not_the_tarball_url_with_a_suffix(self):
        """nextest names the checksum asset after the stem, not the tarball.

        Appending `.sha256` to the tarball URL asks for
        `...-universal-apple-darwin.tar.gz.sha256`, which does not exist --
        a 404 that reads like a missing release rather than a bad URL.
        """
        self.assertNotIn(".tar.gz.sha256", self.text)
        self.assertIn("{self.stem}.sha256", self.text)

    def test_verifies_the_download_before_staging_it(self):
        self.assertIn("verify_checksum(tarball, checksum)", self.text)

    def test_rejects_a_payload_that_is_not_a_macho(self):
        """A same-named Linux binary must not reach the guest."""
        self.assertIn("magic", self.text)
        self.assertIn("Mach-O", self.text)

    def test_matches_the_guest_nextest_to_the_archive_builder(self):
        """nextest owns its archive format; a pinned constant could drift."""
        self.assertIn("nextest_version", self.text)
        self.assertNotRegex(self.text, r"VERSION=\d")


class VerifyGuestResultsTests(unittest.TestCase):
    """The coverage assertion is the point of the lane; pin its behaviour."""

    def setUp(self):
        self.module = load_verify_module()
        self._tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self._tmp.cleanup)
        self.collected = Path(self._tmp.name)

    def write_log(self, *, total=100, passed=100, skipped=0, tests=(), extra=""):
        body = "\n".join(tests)
        log = f"{body}\n{extra}\n     Summary [   1.234s] {total} tests run: {passed} passed, {skipped} skipped\n"
        (self.collected / "nextest.log").write_text(log, encoding="utf-8")

    # Above the collapse floor, so these fixtures exercise the coverage and
    # exit-code assertions rather than tripping the count guard first.
    REALISTIC_TOTAL = 950

    def passing_log(self):
        tests = [
            f"PASS [ 0.1s] kernal-api::snapshot::unwind::frame_pointer_tests::{name}"
            for name in self.module.REQUIRED_X86_64_ONLY_TESTS
        ]
        tests.append("PASS [ 0.1s] kernal-api::crash::tests::off_policy_is_inert")
        self.write_log(total=self.REALISTIC_TOTAL, passed=self.REALISTIC_TOTAL, tests=tests)

    def test_accepts_a_run_that_proves_intel_coverage(self):
        self.passing_log()
        (self.collected / "nextest.rc").write_text("0\n", encoding="utf-8")
        ok, report = self.module.evaluate(self.collected)
        self.assertTrue(ok, report)
        self.assertIn("x86_64-apple-darwin", report)

    def test_rejects_a_green_run_that_executed_no_intel_tests(self):
        self.write_log(
            total=self.REALISTIC_TOTAL,
            passed=self.REALISTIC_TOTAL,
            tests=["PASS [ 0.1s] kernal-api::fs::tests::some_test", "PASS [ 0.1s] kernal-api::crash::tests::x"],
        )
        (self.collected / "nextest.rc").write_text("0\n", encoding="utf-8")
        ok, report = self.module.evaluate(self.collected)
        self.assertFalse(ok)
        self.assertIn("does not prove Intel coverage", report)

    def test_rejects_a_failing_run(self):
        self.passing_log()
        (self.collected / "nextest.rc").write_text("100\n", encoding="utf-8")
        ok, report = self.module.evaluate(self.collected)
        self.assertFalse(ok)
        self.assertIn("nextest exited 100", report)

    def test_rejects_a_collapsed_partition(self):
        tests = [
            f"PASS [ 0.1s] kernal-api::snapshot::unwind::frame_pointer_tests::{name}"
            for name in self.module.REQUIRED_X86_64_ONLY_TESTS
        ]
        tests.append("PASS [ 0.1s] kernal-api::crash::tests::off_policy_is_inert")
        self.write_log(total=6, passed=6, tests=tests)
        (self.collected / "nextest.rc").write_text("0\n", encoding="utf-8")
        ok, report = self.module.evaluate(self.collected)
        self.assertFalse(ok)
        self.assertIn("collapsed", report)

    def test_rejects_missing_evidence(self):
        ok, report = self.module.evaluate(self.collected / "absent")
        self.assertFalse(ok)
        self.assertIn("no collected directory", report)

    def test_reports_guest_staging_failure(self):
        (self.collected / "stage-failure.txt").write_text("could not fetch x\n", encoding="utf-8")
        ok, report = self.module.evaluate(self.collected)
        self.assertFalse(ok)
        self.assertIn("could not fetch x", report)

    def test_requires_every_x86_64_only_unwind_test(self):
        """The list must stay complete: dropping one weakens the proof."""
        self.assertEqual(len(self.module.REQUIRED_X86_64_ONLY_TESTS), 6)
        for name in self.module.REQUIRED_X86_64_ONLY_TESTS:
            self.assertTrue(name.startswith(("frame_pointer_", "recovered_chain_")), name)


if __name__ == "__main__":
    unittest.main()
