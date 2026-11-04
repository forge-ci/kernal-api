"""Regression checks for release lock drift and Cargo diagnostics."""

import subprocess
import tomllib
import unittest
from unittest.mock import patch

from ci import check_compilation_boundary_dependencies as boundary


class CompilationBoundaryTests(unittest.TestCase):
    def test_version_bump_fails_before_cargo_tree(self):
        original = boundary.Path.read_text
        current = tomllib.loads(boundary.Path("Cargo.toml").read_text(encoding="utf-8"))["package"]["version"]

        def bumped(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            return text.replace(f'version = "{current}"', 'version = "999.0.0"') if str(path) == "Cargo.toml" else text

        with patch.object(boundary.Path, "read_text", bumped), patch.object(boundary, "tree") as tree:
            with self.assertRaisesRegex(SystemExit, "dependent lock sync:"):
                boundary.main()
            tree.assert_not_called()

    def test_tracked_path_locks_match_release(self):
        self.assertEqual(boundary.lock_sync_failures(), [])

    def test_cargo_tree_failure_quotes_stderr_without_traceback(self):
        error = subprocess.CalledProcessError(101, ["soldr", "cargo", "tree"], stderr="lock file needs updating")
        with patch.object(boundary.subprocess, "run", side_effect=error):
            with self.assertRaises(SystemExit) as caught:
                boundary.consumer_features("normal")
        self.assertIn("compilation boundary: soldr cargo tree --locked", str(caught.exception))
        self.assertIn("cargo stderr:\nlock file needs updating", str(caught.exception))
