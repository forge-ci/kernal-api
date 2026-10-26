#!/usr/bin/env python3
"""The multi-command steps of ci.yml's `dylints` job (GEN-005).

``select-compiler``
    Point RUSTC/RUSTUP_TOOLCHAIN at the pinned Dylint nightly
    (`--toolchain "$DYLINT_TOOLCHAIN"`) and put ci/dylint-shim on PATH.

``lint-libs-decision --mode M``
    Whether the lint libraries' own suites must run. Each is a ~70 s uncached
    nightly compile, so an ordinary PR that leaves the lints alone skips them.
    Main pushes, full mode, and any PR touching the lints, their shim, or this
    workflow run them. Any failure to fetch or diff keeps `run=true`: a stale
    base only widens the diff, so it errs toward running.

``lanes``
    The required Dylint aggregate (#363). The workspace pass always runs; when
    RUN_LINT_LIBRARY_TESTS is true the two lint libraries' suites run beside it
    (they share no target directory). Each lane logs to its own file, printed
    in a group once all finish, and every failing lane is named.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from ci import gha
except ImportError:  # run as a script: ci/ is sys.path[0]
    import gha

LINT_SURFACES = re.compile(r"^(dylints/|ci/dylint-shim/|\.github/workflows/ci\.yml$)")


@dataclass(frozen=True)
class Lane:
    """One concurrently running Dylint lane."""

    name: str
    command: tuple[str, ...]
    cwd: str | None = None


def select_compiler() -> int:
    toolchain = os.environ["DYLINT_TOOLCHAIN"]
    rustc = gha.stdout_of(["soldr", "rustup", "which", "--toolchain", toolchain, "rustc"])
    gha.set_env("RUSTC", rustc)
    gha.set_env("RUSTUP_TOOLCHAIN", toolchain)
    gha.add_path(str(Path(os.environ.get("GITHUB_WORKSPACE", gha.REPO)) / "ci" / "dylint-shim"))
    return 0


def lint_libraries_unchanged(base_sha: str) -> bool:
    """True only when the PR diff provably leaves every lint surface alone."""
    if not base_sha:
        return False
    if gha.run(["git", "fetch", "--quiet", "--depth=1", "origin", base_sha], check=False) != 0:
        return False
    diff = gha.read(["git", "diff", "--name-only", base_sha, "HEAD"])
    if diff.returncode != 0:
        return False
    return not any(LINT_SURFACES.match(path) for path in diff.lines)


def lint_libs_decision(mode: str) -> int:
    run = True
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request" and mode != "full":
        run = not lint_libraries_unchanged(os.environ.get("BASE_SHA", ""))
    value = "true" if run else "false"
    gha.set_output("run", value)
    print(f"lint-library tests: {value}")
    return 0


def lanes_to_run() -> list[Lane]:
    toolchain = os.environ["DYLINT_TOOLCHAIN"]
    lanes = []
    if os.environ.get("RUN_LINT_LIBRARY_TESTS") == "true":
        # Run from each crate so its .cargo/config.toml applies (RUST-016).
        lanes += [
            Lane(
                "facade-boundary-tests",
                ("soldr", "rustup", "run", toolchain, "cargo", "test"),
                "dylints/kernal_api_boundary",
            ),
            Lane(
                "platform-boundary-tests",
                ("soldr", "rustup", "run", toolchain, "cargo", "test"),
                "dylints/kernal_api_platform_boundary",
            ),
        ]
    lanes.append(
        Lane(
            "workspace-dylint",
            ("soldr", "cargo", "dylint", "--all", "--workspace", "--", "--all-features", "--all-targets"),
        )
    )
    return lanes


def lanes() -> int:
    logs = gha.runner_temp() / "dylint-lanes"
    logs.mkdir(parents=True, exist_ok=True)
    running = []
    for lane in lanes_to_run():
        log = (logs / f"{lane.name}.log").open("w")
        gha.show(list(lane.command))
        running.append(
            (lane, log, subprocess.Popen(lane.command, cwd=lane.cwd, stdout=log, stderr=subprocess.STDOUT))  # pylint: disable=consider-using-with
        )
    failed = []
    for lane, log, process in running:
        if process.wait() != 0:
            failed.append(lane.name)
        log.close()
        gha.group(lane.name)
        print((logs / f"{lane.name}.log").read_text(encoding="utf-8", errors="replace"), flush=True)
        gha.endgroup()
    # This step is the required Dylint aggregate: it fails when any lane
    # fails, and the workspace pass always runs.
    if failed:
        raise gha.StepError(f"Dylint lanes failed: {' '.join(failed)}")
    workspace_log = logs / "workspace-dylint.log"
    if not workspace_log.is_file() or workspace_log.stat().st_size == 0:
        raise gha.StepError("the workspace Dylint pass logged nothing")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ci.yml dylints job steps")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("select-compiler")
    sub.add_parser("lint-libs-decision").add_argument("--mode", required=True)
    sub.add_parser("lanes")
    args = parser.parse_args(sys.argv[1:])
    if args.command == "select-compiler":
        return select_compiler()
    if args.command == "lint-libs-decision":
        return lint_libs_decision(args.mode)
    return lanes()


if __name__ == "__main__":
    gha.main_guard(main)
