"""Keep every Rust package in this repository treating warnings as errors.

The root manifest denies `warnings` for the workspace; each standalone package
(its own `[workspace]`) cannot inherit that table and denies it itself. A new
package, or one that loses its table, would let warnings back in silently.
The generated guest bindings are regenerated wholesale and are excluded.
"""

import subprocess
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATED = {"src/wasm/generated/v1/guest/Cargo.toml"}


def manifests() -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files", "*Cargo.toml"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return [path for path in tracked if path not in GENERATED]


class DenyWarningsTests(unittest.TestCase):
    def test_workspace_denies_warnings(self):
        root = tomllib.loads((ROOT / "Cargo.toml").read_text(encoding="utf-8"))
        self.assertEqual(root["workspace"]["lints"]["rust"]["warnings"], "deny")

    def test_every_package_denies_warnings(self):
        self.assertGreater(len(manifests()), 1)
        for path in manifests():
            with self.subTest(path=path):
                lints = tomllib.loads((ROOT / path).read_text(encoding="utf-8"))["lints"]
                if lints.get("workspace") is True:
                    continue
                self.assertEqual(lints["rust"]["warnings"], "deny")


if __name__ == "__main__":
    unittest.main()
