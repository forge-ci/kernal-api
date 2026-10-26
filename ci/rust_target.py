#!/usr/bin/env python3
"""Materialize a rustup target, repairing stale toolchain-cache bookkeeping.

Usage: ``python ci/rust_target.py [<toolchain>] <target>``

A restored toolchain cache can leave rustup's component list naming a target
whose standard-library archives are gone. rustup then trusts the list:
``target add`` answers "up to date" and installs nothing, and ``target remove``
cannot read the missing manifest. The build later fails as ``E0463: can't find
crate for core``. This verifies the files instead of the bookkeeping, and
repairs by making the bookkeeping true: an empty manifest lets ``remove``
succeed, after which ``add`` really downloads. The manifest is only created
once the target's own libdir is proven incomplete, and an existing manifest is
never overwritten, so a healthy toolchain is never disturbed.

Every Rust tool is reached through Soldr's front door.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class TargetError(RuntimeError):
    """The target could not be materialized; the message says why."""


@dataclass(frozen=True)
class Toolchain:
    """Which toolchain `rustc`/`rustup target` address (`None` = the default)."""

    name: str | None
    target: str
    cwd: Path | None = None

    def rustc(self) -> list[str]:
        if self.name:
            return ["soldr", "rustup", "run", self.name, "rustc"]
        return ["soldr", "rustc"]

    def target_command(self, action: str) -> list[str]:
        if self.name:
            return ["soldr", "rustup", "target", action, "--toolchain", self.name, self.target]
        return ["soldr", "rustup", "target", action, self.target]


def stdout_of(arguments: list[str], *, cwd: Path | None = None) -> str:
    """Run a command, streaming stderr, and return its stdout (iterated live)."""
    with subprocess.Popen(arguments, cwd=cwd, stdout=subprocess.PIPE, text=True) as process:
        assert process.stdout is not None
        text = "".join(process.stdout)
    if process.returncode != 0:
        raise TargetError(f"{' '.join(arguments)} exited {process.returncode}")
    return text


def run(arguments: list[str], *, cwd: Path | None = None) -> None:
    returncode = subprocess.run(arguments, cwd=cwd, check=False).returncode
    if returncode != 0:
        raise TargetError(f"{' '.join(arguments)} exited {returncode}")


def absolute(path: str, what: str) -> Path:
    if not path or not Path(path).is_absolute():
        raise TargetError(f"{what} is not absolute: {path}")
    return Path(path)


def materialized(toolchain: Toolchain) -> bool:
    libdir = absolute(
        stdout_of(
            [*toolchain.rustc(), "--print", "target-libdir", "--target", toolchain.target], cwd=toolchain.cwd
        ).strip(),
        "rustc target-libdir",
    )
    return any(libdir.glob("libcore-*.rlib")) and any(libdir.glob("libstd-*.rlib"))


def ensure_target(toolchain: Toolchain) -> None:
    if materialized(toolchain):
        return
    run(toolchain.target_command("add"), cwd=toolchain.cwd)
    if materialized(toolchain):
        return
    sysroot = absolute(
        stdout_of([*toolchain.rustc(), "--print", "sysroot"], cwd=toolchain.cwd).strip(), "rustc sysroot"
    )
    manifest = sysroot / "lib" / "rustlib" / f"manifest-rust-std-{toolchain.target}"
    if not manifest.exists():
        manifest.touch(exist_ok=False)
    run(toolchain.target_command("remove"), cwd=toolchain.cwd)
    run(toolchain.target_command("add"), cwd=toolchain.cwd)
    if not materialized(toolchain):
        raise TargetError(f"target {toolchain.target} still lacks core/std libraries after reinstall")


def main(argv: list[str]) -> int:
    if len(argv) == 1:
        toolchain = Toolchain(name=None, target=argv[0])
    elif len(argv) == 2:
        toolchain = Toolchain(name=argv[0], target=argv[1])
    else:
        print("usage: rust_target.py [<toolchain>] <target>", file=sys.stderr)
        return 2
    try:
        ensure_target(toolchain)
    except TargetError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
