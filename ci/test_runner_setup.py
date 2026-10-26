"""The background webview apt install stays bounded and retried."""

import unittest

from ci import runner_setup


class AptBackgroundPlanTests(unittest.TestCase):
    def setUp(self):
        self.downloads, self.finish = runner_setup.apt_background_plan(["libgtk-3-dev"])

    def commands(self):
        return [command for attempt in self.downloads for command in attempt] + self.finish

    def test_every_command_is_capped(self):
        for command in self.commands():
            self.assertEqual(command[:2], ["sudo", "timeout"])

    def test_the_install_takes_the_remaining_budget(self):
        self.assertEqual(self.finish[1][2], runner_setup.APT_REMAINING)

    def test_only_the_download_is_retried(self):
        self.assertEqual(len(self.downloads), 2)
        for attempt in self.downloads:
            self.assertEqual(attempt[-1][-4:], ["install", "--yes", "libgtk-3-dev", "--download-only"])
            self.assertIn("Acquire::http::Timeout=30", attempt[-1])
        self.assertIn("update", self.downloads[0][0])

    def test_the_install_repairs_an_interrupted_dpkg_first(self):
        self.assertEqual(self.finish[0][3:], ["dpkg", "--configure", "-a"])
        self.assertEqual(self.finish[1][-3:], ["install", "--yes", "libgtk-3-dev"])

    def test_a_retry_moves_off_the_azure_mirror_and_refreshes(self):
        fallback = self.downloads[1]
        self.assertIn(runner_setup.APT_FALLBACK_MIRROR, " ".join(fallback[0]))
        self.assertIn("azure.archive.ubuntu.com", " ".join(fallback[0]))
        self.assertIn("update", fallback[1])

    def test_fixed_caps_leave_the_install_a_budget_inside_the_deadline(self):
        fixed = sum(int(command[2]) for command in self.commands() if command[2] != runner_setup.APT_REMAINING)
        self.assertLess(fixed + 120, runner_setup.APT_TOTAL_SECONDS)
        self.assertLess(runner_setup.APT_TOTAL_SECONDS, 540)


if __name__ == "__main__":
    unittest.main()
