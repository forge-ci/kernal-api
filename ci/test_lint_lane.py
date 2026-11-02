"""The RUST-033 lint lane mirrors CI's lint flags per target."""

import unittest

from ci.lint_lane import CROSS_TARGETS, HOST_LINT, commands


class LintLaneTests(unittest.TestCase):
    def test_host_runs_full_soldr_lint_with_all_features(self):
        self.assertEqual(HOST_LINT, ("soldr", "lint", "rust", "--host-only", "--all-features"))
        self.assertEqual(commands("a,b")[0], list(HOST_LINT))

    def test_each_cross_target_is_prepared_then_clippied_like_ci(self):
        plan = commands("a,b")[1:]
        self.assertEqual(len(plan), 2 * len(CROSS_TARGETS))
        for index, target in enumerate(CROSS_TARGETS):
            prepare, clippy = plan[2 * index], plan[2 * index + 1]
            self.assertEqual(prepare, ["soldr", "prepare", "--target", target])
            self.assertEqual(
                clippy,
                [
                    "soldr",
                    "cargo",
                    "clippy",
                    "--locked",
                    "--features",
                    "a,b",
                    "--all-targets",
                    "--target",
                    target,
                    "--",
                    "--deny",
                    "warnings",
                ],
            )


if __name__ == "__main__":
    unittest.main()
