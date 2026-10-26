#!/usr/bin/env python3
"""The multi-command steps of ci.yml's `test` job (GEN-005). Nothing compiles.

``run-tests --target T``    replay this host's prebuilt nextest archive,
                            teeing the output to nextest-output.txt.
``prove-own-code``          fail unless this host ran its own platform tree.
``macos-widget --target T`` run the Linux-built AppKit widget proof.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

try:
    from ci import gha
except ImportError:  # run as a script: ci/ is sys.path[0]
    import gha

OUTPUT = Path("nextest-output.txt")
MARKERS = {"Linux": "platform_linux::", "macOS": "platform_macos::", "Windows": "platform_win::"}


def run_tests(target: str) -> int:
    # The filter expression is one argument: `not binary(...)` must reach
    # nextest whole. Source-inspection tests read the Linux builder's tree.
    nextest_filter = ["-E", "not binary(source_policy)"] if os.environ.get("RUNNER_OS") != "Linux" else []
    result = gha.read(
        [
            "cargo-nextest",
            "nextest",
            "run",
            "--archive-file",
            f"kernal-tests-{target}.tar.zst",
            "--workspace-remap",
            ".",
            "--no-fail-fast",
            *nextest_filter,
        ],
        echo=True,
        merge_stderr=True,
    )
    OUTPUT.write_text(result.text + "\n", encoding="utf-8")
    if result.returncode != 0:
        raise gha.StepError(f"cargo-nextest exited {result.returncode}")
    return 0


def prove_own_code() -> int:
    marker = MARKERS.get(os.environ.get("RUNNER_OS", ""), "")
    if not marker or marker not in OUTPUT.read_text(encoding="utf-8", errors="replace"):
        raise gha.StepError(f"no {marker} test ran: this host did not execute its own code")
    return 0


def macos_widget(target: str) -> int:
    binary = Path("target") / target / "debug" / "kernal-tauri-smoke"
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    gha.run([str(binary), "widget"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ci.yml test job steps")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run-tests").add_argument("--target", required=True)
    sub.add_parser("prove-own-code")
    sub.add_parser("macos-widget").add_argument("--target", required=True)
    args = parser.parse_args(sys.argv[1:])
    if args.command == "run-tests":
        return run_tests(args.target)
    if args.command == "macos-widget":
        return macos_widget(args.target)
    return prove_own_code()


if __name__ == "__main__":
    gha.main_guard(main)
