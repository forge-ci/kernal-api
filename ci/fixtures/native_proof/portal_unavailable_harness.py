"""A test harness that wedges the way run 41714637441 attempt 2 did.

It prints libtest's partial ``test <name> ... `` line, then the
xdg-desktop-portal storm a session bus without a display produces, forks a
grandchild (a stand-in for WebKitWebProcess) and blocks forever. The
fixture-driven test asserts the proof driver ends it quickly, names the test
and the portal, and leaves no grandchild behind.
"""

import subprocess
import sys
import time

pid_file = sys.argv[1]
sys.stdout.write("running 15 tests\ntest actual_screenshot_cli_runs_the_offline_guest_and_commits_only_its_output ... ")
sys.stdout.flush()
sys.stderr.write(
    "(xdg-desktop-portal-gtk:9262): Gtk-WARNING **: 00:01:16.818: cannot open display: \n"
    "dbus-daemon[9192]: [session uid=1001 pid=9192] Activated service "
    "'org.freedesktop.impl.portal.desktop.gtk' failed: Process "
    "org.freedesktop.impl.portal.desktop.gtk exited with status 1\n"
)
sys.stderr.flush()
# The grandchild is meant to outlive this process: the proof asserts the driver
# kills it. A context manager would reap it on exit and defeat the fixture.
child = subprocess.Popen(  # pylint: disable=consider-using-with
    [sys.executable, "-c", "import time; time.sleep(600)"]
)
with open(pid_file, "w", encoding="utf-8") as handle:
    handle.write(str(child.pid))
time.sleep(600)
