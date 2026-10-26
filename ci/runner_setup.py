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


def apt_start(packages: list[str]) -> int:
    """Start the install detached; `apt-finish` waits for its status file."""
    temp = gha.runner_temp()
    (temp / APT_STATUS).unlink(missing_ok=True)
    # A tiny Python child runs both apt commands in order and records the
    # final status either way, so the wait below always ends.
    child = (
        "import subprocess, sys\n"
        "status = 0\n"
        f"for command in {apt_commands(packages, APT_RESILIENCE)!r}:\n"
        "    status = subprocess.run(command, check=False).returncode\n"
        "    if status:\n"
        "        break\n"
        f"open({str(temp / APT_STATUS)!r}, 'w').write(str(status))\n"
    )
    log = (temp / APT_LOG).open("w")
    subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-c", child], stdout=log, stderr=subprocess.STDOUT, start_new_session=True
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
