"""Release jobs that build every facility install CI's native -dev packages.

`release.yml` runs `cargo test --all-features`, so it needs every system
library a feature links against. New features add their package to CI first
(audio-capture added `libasound2-dev` there, and the 0.1.37 release then
failed in alsa-sys), so each release apt install must carry at least the
`-dev` packages that every CI Linux lane installs.
"""

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
APT = re.compile(r"runner_setup\.py apt-(?:install|start) (?P<packages>[^\n]+)")


def apt_lines(workflow: str) -> list[set[str]]:
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    return [set(match["packages"].split()) for match in APT.finditer(text)]


def dev_packages(packages: set[str]) -> set[str]:
    return {package for package in packages if package.endswith("-dev")}


class ReleaseSystemPackageTests(unittest.TestCase):
    def test_release_installs_every_dev_package_ci_always_installs(self):
        ci_lines = apt_lines("ci.yml")
        self.assertTrue(ci_lines, "ci.yml no longer installs native packages")
        required = set.intersection(*(dev_packages(line) for line in ci_lines))
        self.assertIn("libasound2-dev", required)

        release_lines = apt_lines("release.yml")
        self.assertTrue(release_lines, "release.yml no longer installs native packages")
        for line in release_lines:
            missing = sorted(required - line)
            self.assertFalse(
                missing,
                f"release.yml apt install is missing CI's native packages: {missing}",
            )


if __name__ == "__main__":
    unittest.main()
