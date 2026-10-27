"""Focused fixtures for the release-only process-substrate guard."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ci" / "check_release_process_substrate.py"
SPEC = importlib.util.spec_from_file_location("release_process_substrate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)

FIXTURES = ROOT / "ci" / "fixtures" / "release_process_substrate"


class ReleaseProcessSubstrateFixtures(unittest.TestCase):
    def findings(self, name: str) -> list[str]:
        return GUARD.running_process_release_violations(GUARD.load_manifest(FIXTURES / name))

    def test_rejects_inline_and_table_dependencies(self) -> None:
        self.assertEqual(
            self.findings("inline-path.toml"),
            ["dependencies.running-process: local path for running-process"],
        )
        self.assertEqual(
            self.findings("table-path.toml"),
            ["dependencies.running-process: local path for running-process"],
        )

    def test_rejects_quoted_and_renamed_dependencies(self) -> None:
        self.assertEqual(
            self.findings("quoted-path.toml"),
            ["dependencies.running-process: local path for running-process"],
        )
        self.assertEqual(
            self.findings("quoted-table-path.toml"),
            ["dependencies.running-process: local path for running-process"],
        )
        self.assertEqual(
            self.findings("alias-path.toml"),
            ["dependencies.process-substrate: local path for running-process"],
        )

    def test_rejects_workspace_target_build_dev_patch_and_replace_dependencies(self) -> None:
        self.assertEqual(
            self.findings("nested-paths.toml"),
            [
                "workspace.dependencies.running-process: local path for running-process",
                "target.cfg(unix).dependencies.running-process: local path for running-process",
                "build-dependencies.process-substrate: local path for running-process",
                "dev-dependencies.running-process: local path for running-process",
                "patch.crates-io.running-process: running-process patch/replacement is forbidden",
                "replace.running-process:4.10.9: running-process patch/replacement is forbidden",
            ],
        )

    def test_rejects_git_nonexact_wrong_and_zero_registry_forms(self) -> None:
        self.assertEqual(
            self.findings("invalid-registry.toml"),
            [
                "dependencies.git-process: non-registry Git source for running-process",
                "dependencies.caret-process: running-process must use exact registry version =4.10.20",
                "dependencies.wrong-process: running-process must use exact registry version =4.10.20",
                "dependencies.zero-process: running-process must use exact registry version =4.10.20",
                "dependencies.missing-process: running-process must use exact registry version =4.10.20",
            ],
        )

    def test_allows_registry_only_dependency_forms(self) -> None:
        self.assertEqual(self.findings("registry-only.toml"), [])

    def test_allows_package_metadata_that_is_not_a_dependency(self) -> None:
        self.assertEqual(self.findings("metadata-path.toml"), [])


if __name__ == "__main__":
    unittest.main()
