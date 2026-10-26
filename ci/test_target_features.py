"""The per-target build graph: every feature except the host-only ones."""

import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

from ci import target_features

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


class TargetFeatureTests(unittest.TestCase):
    def test_every_feature_but_default_and_host_only(self):
        features = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))["features"]
        expected = sorted(set(features) - {"default"} - target_features.HOST_ONLY)
        self.assertEqual(target_features.target_features(), expected)
        self.assertIn("build-resources", target_features.HOST_ONLY)
        self.assertNotIn("build-resources", target_features.target_features())
        self.assertIn("tauri-webview", target_features.target_features())

    def test_there_are_no_implicit_optional_dependency_features(self):
        """`--all-features` would also enable those; the list must be all of it."""
        manifest = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))
        tables = [manifest["dependencies"]] + [
            platform.get("dependencies", {}) for platform in manifest.get("target", {}).values()
        ]
        optional = {name for table in tables for name, spec in table.items()
                    if isinstance(spec, dict) and spec.get("optional")}
        named = {entry[4:] for entries in manifest["features"].values()
                 for entry in entries if entry.startswith("dep:")}
        self.assertEqual(optional - named, set())

    def test_script_prints_a_comma_list(self):
        printed = subprocess.run(
            [sys.executable, str(ROOT / "ci/target_features.py")], check=True, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(printed.split(","), target_features.target_features())

    def test_the_build_matrix_links_the_target_graph_not_all_features(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        start = workflow.index("    name: Build (${{ matrix.target }})")
        build = workflow[start:workflow.index("\n  test:\n", start)]
        self.assertIn("ci/target_features.py", build)
        self.assertNotIn("--all-features", build)
        # Lint and the smoke binary inline; the archive's two builds in ci/build_steps.py.
        self.assertEqual(len(re.findall(r'--features "\$\{KERNAL_TARGET_FEATURES\}"', build)), 2)
        steps = (ROOT / "ci/build_steps.py").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r'"--features",\s*features\(\)', steps)), 2)
        self.assertNotIn("--all-features", steps)
        self.assertIn("ci/build_steps.py archive", build)
        # build-resources keeps real coverage where it runs: a build script.
        self.assertIn("ci/build_steps.py embed-resources", build)
        self.assertRegex(steps, r'"--manifest-path",\s*"tests/build-resources-consumer/Cargo.toml"')


if __name__ == "__main__":
    unittest.main()
