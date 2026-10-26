"""Shared helpers for the workflow-step scripts (GEN-005: no shell in `run:`).

Every workflow `run:` step is one line calling a `ci/*.py` script; the control
flow those steps used to carry inline lives in the scripts, built on these
helpers. Standard library only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class StepError(RuntimeError):
    """A step failed; the message is printed as a GitHub `::error::`."""


@dataclass(frozen=True)
class Output:
    """A finished command's exit status and stdout lines."""

    returncode: int
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def show(arguments: list[str]) -> None:
    print("+ " + " ".join(arguments), flush=True)


def run(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> int:
    """Run a command with inherited stdio; raise on failure when `check`."""
    show(arguments)
    returncode = subprocess.run(arguments, cwd=cwd, env=env, check=False).returncode
    if check and returncode != 0:
        raise StepError(f"{' '.join(arguments)} exited {returncode}")
    return returncode


def read(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    echo: bool = False,
    merge_stderr: bool = False,
) -> Output:
    """Run a command and collect its stdout by iterating the live pipe."""
    show(arguments)
    with subprocess.Popen(
        arguments,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else None,
        text=True,
        errors="replace",
    ) as process:
        assert process.stdout is not None
        lines = []
        for line in process.stdout:
            if echo:
                sys.stdout.write(line)
                sys.stdout.flush()
            lines.append(line.rstrip("\r\n"))
    return Output(process.returncode, tuple(lines))


def stdout_of(arguments: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    """The stripped stdout of a command that must succeed."""
    output = read(arguments, cwd=cwd, env=env)
    if output.returncode != 0:
        raise StepError(f"{' '.join(arguments)} exited {output.returncode}")
    return output.text.strip()


def _append(variable: str, line: str) -> None:
    path = os.environ.get(variable)
    if not path:
        raise StepError(f"{variable} is not set; run this inside a GitHub Actions step")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def set_env(name: str, value: str) -> None:
    """Export `name=value` to every later step of this job (GITHUB_ENV)."""
    _append("GITHUB_ENV", f"{name}={value}")


def set_output(name: str, value: str) -> None:
    _append("GITHUB_OUTPUT", f"{name}={value}")


def add_path(directory: str) -> None:
    _append("GITHUB_PATH", directory)


def runner_temp() -> Path:
    return Path(os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR") or "/tmp")


def group(title: str) -> None:
    print(f"::group::{title}", flush=True)


def endgroup() -> None:
    print("::endgroup::", flush=True)


def main_guard(function) -> None:  # type: ignore[no-untyped-def]
    """Run `function()`, turning a StepError into `::error::` and exit 1."""
    try:
        code = function()
    except StepError as error:
        print(f"::error::{error}", flush=True)
        sys.exit(1)
    sys.exit(code or 0)
