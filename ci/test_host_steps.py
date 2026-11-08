"""Guards in ci/host_steps.py."""

from __future__ import annotations

import unittest

try:
    from ci import gha, host_steps
except ImportError:  # run as a script: ci/ is sys.path[0]
    import gha
    import host_steps


class OfflineGuardTest(unittest.TestCase):
    def test_ci_rejects_offline_variable(self) -> None:
        for value in ("1", ""):
            with self.subTest(value=value):
                with self.assertRaises(gha.StepError) as raised:
                    host_steps.check_offline_guard({"CI": "true", host_steps.OFFLINE_VAR: value})
                self.assertIn(host_steps.OFFLINE_VAR, str(raised.exception))

    def test_offline_allowed_outside_ci(self) -> None:
        host_steps.check_offline_guard({host_steps.OFFLINE_VAR: "1"})
        host_steps.check_offline_guard({"CI": "", host_steps.OFFLINE_VAR: "1"})

    def test_ci_without_offline_passes(self) -> None:
        host_steps.check_offline_guard({"CI": "true"})


if __name__ == "__main__":
    unittest.main()
