#!/usr/bin/env python3
"""Runner provisioning steps for the CI workflows (GEN-005).

Subcommands:

``apt-install <pkg>...``       ``sudo apt-get update`` then ``install --yes``.
``apt-start <pkg>...``         the same, in the background, with bounded
                               mirror retries/timeouts; status and log land in
                               $RUNNER_TEMP (a step's background process lives
                               until the job ends).
``apt-finish``                 wait (<= 9 min) for ``apt-start``, print its log,
                               fail unless it exited 0.
``pkg-config-path [--target]`` export the runner's native pkg-config search
                               path for one target triple.
``nextest-install``            the cargo-nextest build this OS/arch executes.
``refs-volume``                format and mount a ReFS volume (Windows).
``screenshot-storage``         export KERNAL_API_SCREENSHOT_PROOF_DIR.
``webview2``                   ensure the Evergreen WebView2 Runtime (Windows).
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

try:
    from ci import gha
except ImportError:  # run as a script: ci/ is sys.path[0]
    import gha

APT_RESILIENCE = [
    "-o",
    "Acquire::Retries=3",
    "-o",
    "Acquire::http::Timeout=30",
    "-o",
    "Acquire::https::Timeout=30",
]
APT_STATUS = "webview-packages.status"
APT_LOG = "webview-packages.log"
# Pick by OS *and* architecture. The bare `linux` and `windows` builds are
# x86-64: on the ARM runners they fail with `Exec format error` before a
# single test starts. `mac` is universal.
NEXTEST_URLS = {
    "Linux-X64": "https://get.nexte.st/latest/linux",
    "Linux-ARM64": "https://get.nexte.st/latest/linux-arm",
    "Windows-X64": "https://get.nexte.st/latest/windows",
    "Windows-ARM64": "https://get.nexte.st/latest/windows-arm",
    "macOS-X64": "https://get.nexte.st/latest/mac",
    "macOS-ARM64": "https://get.nexte.st/latest/mac",
}
WEBVIEW2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_BOOTSTRAPPER = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def apt_commands(packages: list[str], options: list[str]) -> list[list[str]]:
    return [
        ["sudo", "apt-get", *options, "update"],
        ["sudo", "apt-get", *options, "install", "--yes", *packages],
    ]


def apt_install(packages: list[str]) -> int:
    for command in apt_commands(packages, []):
        gha.run(command)
    return 0


# A mirror can trickle a large package (libwebkit2gtk-4.1-0 is ~25 MB) so
# slowly that it never trips Acquire::*::Timeout, which only fires on a silent
# connection. Only the download is capped and retried: killing it is safe and
# apt resumes the partial .deb from its archive cache. Killing dpkg mid-unpack
# is not (later attempts fail with "dpkg was interrupted"), so the offline
# install, after `dpkg --configure -a`, gets whatever remains of one overall
# budget measured from when the child starts: unpacking can take minutes on a
# slow disk, and a fast download should leave that time to it. The budget ends
# before apt-finish's 540 s deadline, which starts later than the child.
APT_UPDATE_SECONDS = 60
APT_DOWNLOAD_SECONDS = 100
APT_MIRROR_SECONDS = 10
APT_CONFIGURE_SECONDS = 10
APT_TOTAL_SECONDS = 530
# Stands in for the install's timeout until the child knows what is left.
APT_REMAINING = "{remaining}"
# GitHub's Azure Ubuntu mirror has trickled the 25 MB webkit .deb past every
# capped attempt (runs 40368020419, 40374990547); the retry uses the primary.
APT_FALLBACK_MIRROR = "archive.ubuntu.com"


def capped(seconds: int, command: list[str]) -> list[str]:
    return ["sudo", "timeout", str(seconds), *command[1:]]


def apt_background_plan(packages: list[str]) -> tuple[list[list[list[str]]], list[list[str]]]:
    """apt-start's child: download attempts until one succeeds, then the install.

    The first download attempt updates and fetches. The second moves the
    sources off the Azure mirror and updates again before fetching.
    """
    update, install = apt_commands(packages, APT_RESILIENCE)
    fetch = capped(APT_DOWNLOAD_SECONDS, [*install, "--download-only"])
    capped_update = capped(APT_UPDATE_SECONDS, update)
    switch_mirror = [
        "sudo",
        "timeout",
        str(APT_MIRROR_SECONDS),
        "sh",
        "-c",
        "grep -rl azure.archive.ubuntu.com /etc/apt"
        f" | xargs -r sed -i 's/azure\\.archive\\.ubuntu\\.com/{APT_FALLBACK_MIRROR}/g'",
    ]
    downloads = [
        [capped_update, fetch],
        [switch_mirror, capped_update, fetch],
    ]
    finish = [
        ["sudo", "timeout", str(APT_CONFIGURE_SECONDS), "dpkg", "--configure", "-a"],
        ["sudo", "timeout", APT_REMAINING, *install[1:]],
    ]
    return downloads, finish


def apt_start(packages: list[str]) -> int:
    """Start the install detached; `apt-finish` waits for its status file."""
    temp = gha.runner_temp()
    (temp / APT_STATUS).unlink(missing_ok=True)
    downloads, finish = apt_background_plan(packages)
    # A tiny Python child retries the capped download until one attempt
    # succeeds, installs offline in the remaining budget, and records the final status either way, so
    # the wait below always ends.
    child = (
        "import subprocess, time\n"
        "started = time.monotonic()\n"
        "def run(commands):\n"
        "    for command in commands:\n"
        f"        remaining = int({APT_TOTAL_SECONDS} - (time.monotonic() - started))\n"
        f"        if {APT_REMAINING!r} in command:\n"
        "            if remaining <= 0:\n"
        "                return 124\n"
        f"            command = [str(remaining) if part == {APT_REMAINING!r} else part for part in command]\n"
        "        status = subprocess.run(command, check=False).returncode\n"
        "        if status != 0:\n"
        "            return status\n"
        "    return 0\n"
        f"for attempt in {downloads!r}:\n"
        "    status = run(attempt)\n"
        "    if status == 0:\n"
        "        break\n"
        "    print(f'apt download attempt exited {status}; retrying', flush=True)\n"
        "if status == 0:\n"
        f"    status = run({finish!r})\n"
        f"open({str(temp / APT_STATUS)!r}, 'w').write(str(status))\n"
    )
    log = (temp / APT_LOG).open("w")
    subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-c", child],
        stdin=subprocess.DEVNULL,  # as bash does for `( ... ) &`: never hold the step's stdin
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"webview package install started in the background; log: {temp / APT_LOG}")
    return 0


def apt_finish() -> int:
    temp = gha.runner_temp()
    status = temp / APT_STATUS
    # Give up a minute before the step timeout so the log still prints.
    deadline = time.monotonic() + 540
    while not (status.is_file() and status.stat().st_size) and time.monotonic() < deadline:
        time.sleep(1)
    log = temp / APT_LOG
    if log.is_file():
        print(log.read_text(encoding="utf-8", errors="replace"))
    if not (status.is_file() and status.stat().st_size):
        raise gha.StepError("webview package install did not finish within 9 minutes")
    if status.read_text(encoding="utf-8").strip() != "0":
        raise gha.StepError(f"webview package install exited {status.read_text(encoding='utf-8').strip()}")
    return 0


def pkg_config_path(target: str) -> int:
    multiarch = gha.stdout_of(["dpkg-architecture", "-qDEB_HOST_MULTIARCH"])
    # Only the variable NAME takes underscores; the path keeps hyphens.
    gha.set_env(
        f"PKG_CONFIG_LIBDIR_{target.replace('-', '_')}",
        f"/usr/lib/{multiarch}/pkgconfig:/usr/share/pkgconfig",
    )
    return 0


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read()


def nextest_install() -> int:
    key = f"{os.environ.get('RUNNER_OS', '')}-{os.environ.get('RUNNER_ARCH', '')}"
    url = NEXTEST_URLS.get(key)
    if url is None:
        raise gha.StepError(f"no cargo-nextest build for {key}")
    destination = Path.home() / ".cargo" / "bin"
    destination.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    payload = download(url)
    if key.startswith("Windows-"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            archive.extractall(destination)
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            archive.extractall(destination, filter="data")
    gha.add_path(str(destination))
    return 0


def refs_volume() -> int:
    """Format a ReFS volume so extent-sharing tests prove block clones (Windows)."""
    temp = gha.runner_temp()
    vhd = temp / "refs.vhdx"
    script = temp / "refs-diskpart.txt"
    script.write_text(
        "\n".join(
            [
                f'create vdisk file="{vhd}" maximum=2048 type=expandable',
                f'select vdisk file="{vhd}"',
                "attach vdisk",
                "create partition primary",
                "format fs=refs quick label=KernalReFS",
                "assign letter=R",
            ]
        )
        + "\n",
        encoding="ascii",
    )
    returncode = gha.run(["diskpart", "/s", str(script)], check=False)
    if returncode != 0:
        raise gha.StepError(f"diskpart failed: {returncode}")
    info = gha.read(["fsutil", "fsinfo", "volumeinfo", "R:"])
    names = [line.split(":", 1)[1].strip() for line in info.lines if line.startswith("File System Name")]
    if names != ["ReFS"]:
        raise gha.StepError(f"R: is {names}, not ReFS")
    Path("R:\\kernal-refs").mkdir()
    gha.set_env("KERNAL_REFS_TEST_DIR", "R:\\kernal-refs")
    return 0


def screenshot_storage() -> int:
    gha.set_env("KERNAL_API_SCREENSHOT_PROOF_DIR", os.path.join(str(gha.runner_temp()), "kernal-api-screenshot-proof"))
    return 0


def webview2_version() -> str | None:
    """Microsoft's documented Evergreen Runtime keys, not the Edge browser."""
    import winreg  # pylint: disable=import-outside-toplevel,import-error

    for hive, key in (
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + WEBVIEW2_CLIENT),
        (winreg.HKEY_CURRENT_USER, "Software\\" + WEBVIEW2_CLIENT),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value = str(winreg.QueryValueEx(handle, "pv")[0])
        except OSError:
            continue
        parts = value.split(".")
        if parts and all(part.isdigit() for part in parts) and any(int(part) for part in parts):
            return value
    return None


def authenticode_valid(path: Path) -> bool:
    status = gha.stdout_of(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-AuthenticodeSignature -LiteralPath '{path}').Status",
        ]
    )
    return status == "Valid"


def webview2() -> int:
    if not webview2_version():
        installer = gha.runner_temp() / "MicrosoftEdgeWebview2Setup.exe"
        installer.write_bytes(download(WEBVIEW2_BOOTSTRAPPER))
        if not authenticode_valid(installer):
            raise gha.StepError("Invalid WebView2 installer signature")
        returncode = gha.run([str(installer), "/silent", "/install"], check=False)
        if returncode != 0:
            raise gha.StepError(f"WebView2 installation failed: {returncode}")
    version = webview2_version()
    if not version:
        raise gha.StepError("WebView2 Runtime is unavailable after setup")
    print(f"WebView2 Runtime: {version}")
    return 0


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CI runner provisioning steps")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("apt-install", "apt-start"):
        sub.add_parser(name).add_argument("packages", nargs="+")
    sub.add_parser("apt-finish")
    sub.add_parser("pkg-config-path").add_argument("--target", default="x86_64-unknown-linux-gnu")
    for name in ("nextest-install", "refs-volume", "screenshot-storage", "webview2"):
        sub.add_parser(name)
    return parser.parse_args(argv)


def main() -> int:
    args = parse(sys.argv[1:])
    if args.command == "apt-install":
        return apt_install(args.packages)
    if args.command == "apt-start":
        return apt_start(args.packages)
    simple = {
        "apt-finish": apt_finish,
        "nextest-install": nextest_install,
        "refs-volume": refs_volume,
        "screenshot-storage": screenshot_storage,
        "webview2": webview2,
    }
    if args.command in simple:
        return simple[args.command]()
    return pkg_config_path(args.target)


if __name__ == "__main__":
    gha.main_guard(main)
