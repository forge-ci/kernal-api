"""The background webview apt install stays bounded and retried."""

import io
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

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


class DownloadTests(unittest.TestCase):
    url = "https://example.com/asset"

    def http_error(self, status: int):
        return urllib.error.HTTPError(self.url, status, "CDN error", Message(), None)

    def test_504_then_success(self):
        with (
            patch.object(
                runner_setup.urllib.request, "urlopen", side_effect=[self.http_error(504), io.BytesIO(b"asset")]
            ) as request,
            patch.object(runner_setup.time, "sleep") as sleep,
            patch("builtins.print") as log,
        ):
            self.assertEqual(runner_setup.download(self.url), b"asset")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once()
        warning = log.call_args.args[0]
        self.assertIn("##[warning]", warning)
        self.assertIn("1/6", warning)
        self.assertIn("504", warning)
        self.assertIn("retrying in", warning)

    def test_attempts_exhausted(self):
        with (
            patch.object(runner_setup.urllib.request, "urlopen", side_effect=self.http_error(504)) as request,
            patch.object(runner_setup.time, "sleep") as sleep,
            self.assertRaisesRegex(runner_setup.gha.StepError, f"{self.url} failed after 6 attempts"),
        ):
            runner_setup.download(self.url)
        self.assertEqual(request.call_count, 6)
        self.assertEqual(sleep.call_count, 5)

    def test_4xx_is_not_retried(self):
        for status in (400, 401, 403, 404, 429):
            with (
                self.subTest(status=status),
                patch.object(runner_setup.urllib.request, "urlopen", side_effect=self.http_error(status)) as request,
                patch.object(runner_setup.time, "sleep") as sleep,
                self.assertRaises(urllib.error.HTTPError),
            ):
                runner_setup.download(self.url)
            request.assert_called_once()
            sleep.assert_not_called()

    def test_network_errors_are_retried(self):
        for error in (urllib.error.URLError("connection lost"), TimeoutError(), ConnectionResetError()):
            with (
                self.subTest(error=error),
                patch.object(runner_setup.urllib.request, "urlopen", side_effect=[error, io.BytesIO(b"ok")]),
                patch.object(runner_setup.time, "sleep") as sleep,
            ):
                self.assertEqual(runner_setup.download(self.url), b"ok")
            sleep.assert_called_once()

    def test_elapsed_budget_caps_slow_attempts_and_backoff(self):
        clock = [0.0]
        delays: list[float] = []
        timeouts: list[float] = []

        def request(url: str, timeout: float):
            timeouts.append(timeout)
            clock[0] += timeout
            raise self.http_error(504)

        def sleep(delay: float):
            delays.append(delay)
            clock[0] += delay

        with (
            patch.object(runner_setup.urllib.request, "urlopen", side_effect=request),
            patch.object(runner_setup.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(runner_setup.time, "sleep", side_effect=sleep),
            patch.object(runner_setup.random, "uniform", return_value=1),
            self.assertRaises(runner_setup.gha.StepError),
        ):
            runner_setup.download(self.url)
        self.assertLessEqual(clock[0], runner_setup.DOWNLOAD_BUDGET_SECONDS)
        self.assertEqual(runner_setup.DOWNLOAD_BUDGET_SECONDS, 180)
        self.assertLessEqual(len(timeouts), 6)
        self.assertGreaterEqual(timeouts[0], 120)
        self.assertEqual(delays, [])
        self.assertLessEqual(sum(delays) + sum(timeouts), 180)

    def test_digest_mismatch_is_not_retried(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(runner_setup.Path, "home", return_value=Path(temporary)),
            patch.dict(runner_setup.os.environ, {"RUNNER_OS": "Linux", "RUNNER_ARCH": "X64"}),
            patch.object(runner_setup.urllib.request, "urlopen", return_value=io.BytesIO(b"bad digest")) as request,
            patch.object(runner_setup.time, "sleep") as sleep,
            self.assertRaisesRegex(runner_setup.gha.StepError, "digest mismatch"),
        ):
            runner_setup.nextest_install()
        request.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
