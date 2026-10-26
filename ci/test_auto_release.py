"""Contract checks for autonomous release orchestration."""

import unittest
import subprocess
import tempfile
import tomllib
from pathlib import Path
from unittest.mock import patch

from auto_release import main, release_tag, should_release

ROOT = Path(__file__).resolve().parents[1]


class AutoReleaseTests(unittest.TestCase):
    def test_tracked_consumer_versions_match_release(self):
        version = tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"]
        with tempfile.TemporaryFile(mode="w+") as output:
            subprocess.run(
                ["git", "ls-files", "**/Cargo.toml", "**/Cargo.lock", "Cargo.lock"],
                cwd=ROOT, stdout=output, check=True,
            )
            output.seek(0)
            tracked = output.read().splitlines()
        for filename in tracked:
            document = tomllib.loads((ROOT / filename).read_text())
            with self.subTest(path=filename):
                if filename.endswith("Cargo.lock"):
                    for package in document.get("package", []):
                        if package["name"] == "kernal-api":
                            self.assertEqual(package["version"], version)
                else:
                    for section in ("dependencies", "build-dependencies", "dev-dependencies"):
                        dependency = document.get(section, {}).get("kernal-api")
                        if isinstance(dependency, dict) and "path" in dependency:
                            if "version" in dependency:
                                self.assertEqual(dependency["version"], f"={version}")

    def test_version_detection(self):
        self.assertEqual(release_tag('[package]\nversion = "0.1.0"'), "v0.1.0")
        for version in ["0.0.0", "bad", "0.1.0\nmalicious"]:
            with self.assertRaises(ValueError):
                release_tag(f'[package]\nversion = "{version}"')
        self.assertTrue(should_release("v0.1.0", "v0.0.0", False))
        self.assertFalse(should_release("v0.1.0", "v0.1.0", False))
        self.assertFalse(should_release("v0.1.0", None, False))
        self.assertTrue(should_release("v0.1.0", "v0.1.0", True))

    def test_workflow_has_safe_automatic_and_manual_entrypoints(self):
        workflow = (ROOT / ".github/workflows/auto-release.yml").read_text()
        self.assertNotIn("branches: [main]", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("candidate_sha:", workflow)
        self.assertIn("full_ci_run_id:", workflow)
        self.assertIn("ci/release_ci_gate.py", workflow)
        self.assertIn("needs: [prepare, full-ci-gate]", workflow)
        self.assertIn("default: true", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("uses: ./.github/workflows/release.yml", workflow)
        self.assertIn("ref: ${{ inputs.candidate_sha }}", workflow)

    def test_registry_publish_is_explicitly_opt_in(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("workflow_call:", workflow)
        self.assertNotIn("github.event.release.tag_name", workflow)
        self.assertIn("vars.PUBLISH_PYPI == 'true'", workflow)
        self.assertIn("needs: [validate-and-package, release-assets]", workflow)
        caller = (ROOT / ".github/workflows/auto-release.yml").read_text()
        self.assertIn("vars.PUBLISH_CRATES_IO == 'true'", caller)
        self.assertIn("needs: [prepare, full-ci-gate, release]", caller)
        self.assertIn("!inputs.dry_run", caller)
        self.assertIn("!inputs.dry_run", workflow)
        self.assertNotIn("--clobber", workflow)
        self.assertIn("path: registry-packages/*", workflow)
        self.assertIn("ci/release_steps.py stage-registry", workflow)
        steps = (ROOT / "ci/release_steps.py").read_text()
        self.assertIn('glob.glob("target/package/kernal-api-*.crate")', steps)
        self.assertIn('glob.glob("dist/*")', steps)
        self.assertIn('Path("registry-packages")', steps)

    def test_every_release_job_compiles_on_linux(self):
        """No release build runs on an Apple or Windows host (#273)."""
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertNotRegex(workflow, r"(?m)^\s+(- )?os: (windows|macos)-")
        self.assertNotRegex(workflow, r"(?m)^\s+runs-on: (windows|macos)-")
        workers = workflow.split("\n  symbolizer-workers:\n", 1)[1].split("\n  publish-", 1)[0]
        for target in ("x86_64-pc-windows-msvc", "aarch64-pc-windows-msvc"):
            with self.subTest(target=target):
                entry = workers.split(f"target: {target}", 1)[1].split("- os:", 1)[0]
                self.assertIn("cross: true", entry)

    def test_release_stages_the_generated_conpty_manifest(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertNotIn(
            "cmp conpty-sidecar.sha256.toml target/conpty-assets/conpty-sidecar.sha256.toml",
            workflow,
        )
        self.assertIn(
            "cp target/conpty-assets/conpty-sidecar.sha256.toml conpty-sidecar.sha256.toml",
            workflow,
        )
        self.assertIn(
            "target/conpty-assets/conpty-sidecar.sha256.toml",
            workflow,
        )
        self.assertIn(
            "ci/verify_conpty_assets.py target/conpty-assets", workflow
        )
        self.assertIn("uv run --no-project --python 3.12 ci/crate_release.py package", workflow)
        caller = (ROOT / ".github/workflows/auto-release.yml").read_text()
        self.assertIn("ci/crate_release.py publish", caller)
        script = (ROOT / "ci/crate_release.py").read_text()
        self.assertIn('"package", "--locked", "--all-features", "--allow-dirty"', script)
        self.assertIn('"publish", "--locked", "--no-verify", "--allow-dirty"', script)
        self.assertIn(
            "cp conpty-sidecars/conpty-sidecar.sha256.toml conpty-sidecar.sha256.toml",
            caller,
        )

    def test_crates_io_publishes_through_trusted_publishing(self):
        """crates.io trusts `auto-release.yml`, so it publishes there, by OIDC.

        The first real release failed at `please provide a non-empty token`:
        the job read a CARGO_REGISTRY_TOKEN secret that was never set, while
        the crate's trusted publisher (zackees/kernal-api, auto-release.yml)
        sat unused.
        """
        caller = (ROOT / ".github/workflows/auto-release.yml").read_text()
        called = (ROOT / ".github/workflows/release.yml").read_text()
        job = caller.split("\n  publish-crates:\n", 1)[1]
        self.assertIn("id-token: write", job)
        self.assertIn("uses: rust-lang/crates-io-auth-action@", job)
        self.assertIn("CARGO_REGISTRY_TOKEN: ${{ steps.crates-io-auth.outputs.token }}", job)
        self.assertLess(job.index("crates-io-auth-action"), job.index("ci/crate_release.py publish"))
        for workflow in (caller, called):
            self.assertNotIn("secrets.CARGO_REGISTRY_TOKEN", workflow)
        self.assertNotIn("cargo publish", called)

    def test_shipping_jobs_restore_no_cache(self):
        """What ships is built from sources, never from a cache another job wrote.

        v0.1.14's aarch64 Linux worker restored a shared `tnone` toolchain
        entry saved by the previous release's aarch64 job: its rustup records
        listed the aarch64 std installed mid-build, the archive did not hold
        it, and the build failed at E0463. `cache: false` alone does not reach
        the toolchain cache, so every layer is named. `validate-and-package`
        may cache: it tests, and packages sources a cache cannot alter.
        """
        called = (ROOT / ".github/workflows/release.yml").read_text()
        caller = (ROOT / ".github/workflows/auto-release.yml").read_text()
        for text, job in ((called, "symbolizer-workers"), (caller, "publish-crates")):
            body = text.split(f"\n  {job}:\n", 1)[1].split("\n\n  ", 1)[0]
            steps = body.split("uses: ./.github/actions/soldr")[1:]
            with self.subTest(job=job):
                self.assertTrue(steps)
                for step in steps:
                    block = step.split("\n      - ", 1)[0]
                    self.assertIn("cache: false", block)
                    self.assertIn("build-cache: false", block)
                    self.assertIn("solo-toolchain-cache: false", block)

    def test_a_failed_release_job_stops_the_release(self):
        called = (ROOT / ".github/workflows/release.yml").read_text()
        caller = (ROOT / ".github/workflows/auto-release.yml").read_text()
        self.assertNotIn("fail-fast: false", called)
        self.assertIn("fail-fast: true", called)
        for text, jobs in (
            (called, ("release-guard", "validate-and-package", "symbolizer-workers", "publish-pypi", "release-assets")),
            (caller, ("publish-crates",)),
        ):
            for job in jobs:
                with self.subTest(job=job):
                    body = text.split(f"\n  {job}:\n", 1)[1].split("\n\n  ", 1)[0]
                    last = body.rsplit("\n      - ", 1)[1]
                    self.assertIn("name: Cancel the run (this job failed)", last)
                    self.assertIn("if: failure()", last)
                    self.assertIn("gh run cancel ${{ github.run_id }}", last)
                    # Each job grants itself the cancel permission; the
                    # workflow default stays read-only (ci-lint SEC-002).
                    self.assertIn("actions: write", body.split("\n    steps:\n", 1)[0])
            self.assertIn(
                "\npermissions:\n  contents: read\n  actions: read\n",
                text.split("\njobs:\n", 1)[0],
            )
        # A called workflow gets no more than its caller grants.
        release_call = caller.split("\n  release:\n", 1)[1].split("\n\n  ", 1)[0]
        self.assertIn("actions: write", release_call)
        self.assertIn("contents: write", release_call)

    def verify_source(self, tag="v0.1.0", sha="a" * 40, tagged_sha=None):
        env = {"RELEASE_TAG": tag, "RELEASE_SHA": sha, "GITHUB_SHA": "a" * 40}
        results = ["a" * 40, "" if tagged_sha is None else tag, tagged_sha]
        with (
            patch("sys.argv", ["auto_release.py", "--verify-source"]),
            patch.dict("os.environ", env),
            patch.object(Path, "read_text", return_value='[package]\nversion="0.1.0"'),
            patch("subprocess.check_output", side_effect=results),
        ):
            main()

    def test_source_guard_accepts_absent_or_matching_tag(self):
        self.verify_source()
        self.verify_source(tagged_sha="a" * 40)

    def test_source_guard_rejects_version_commit_and_existing_tag_mismatch(self):
        for arguments in [
            {"tag": "v0.2.0"},
            {"sha": "b" * 40},
            {"tagged_sha": "b" * 40},
        ]:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.verify_source(**arguments)


if __name__ == "__main__":
    unittest.main()
