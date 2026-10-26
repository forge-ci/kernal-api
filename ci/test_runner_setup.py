"""The background webview apt install stays bounded and retried."""

import unittest

from ci import runner_setup


class AptBackgroundPlanTests(unittest.TestCase):
    def test_every_command_is_capped_and_the_install_is_retried(self):
        plan = runner_setup.apt_background_plan(["libgtk-3-dev"])
        self.assertEqual(len(plan), 1 + runner_setup.APT_INSTALL_ATTEMPTS)
        for command in plan:
            self.assertEqual(command[:2], ["sudo", "timeout"])
            self.assertIn("apt-get", command)
            self.assertIn("Acquire::http::Timeout=30", command)
        self.assertIn("update", plan[0])
        for command in plan[1:]:
            self.assertEqual(command[-3:], ["install", "--yes", "libgtk-3-dev"])

    def test_worst_case_ends_before_the_finish_deadline(self):
        worst = runner_setup.APT_UPDATE_SECONDS + runner_setup.APT_INSTALL_ATTEMPTS * runner_setup.APT_INSTALL_SECONDS
        self.assertLess(worst, 540)


if __name__ == "__main__":
    unittest.main()
