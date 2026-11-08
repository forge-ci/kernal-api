#!/usr/bin/env python3
"""The merge-queue-tier gate jobs of ci.yml: lockfiles, wasm-guests, desktop-status.

Each was a full-only step inside `linux` and landed a deterministic defect on
main through the queue (runs 41989719729, 41770081641, 42301454109,
42005878882). They are cheap (seconds to ~9 minutes), so they run as their own
jobs in the `test` tier (merge_group, `ci-test` PRs) and in `full`, where
`CI summary` sees each job's real conclusion. Every Rust tool runs via Soldr.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from ci import linux_steps, native_proof
except ImportError:  # run as a script: ci/ is sys.path[0]
    import linux_steps
    import native_proof


def lockfiles() -> int:
    linux_steps.dependency_boundary()
    linux_steps.external_consumers()
    linux_steps.release_tooling()
    return 0


def wasm_guests(out: Path, work: Path) -> int:
    native_proof.guests(out, work)
    return 0


def desktop_status() -> int:
    return linux_steps.desktop_status(full=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("lockfiles")
    sub.add_parser("desktop-status")
    wasm = sub.add_parser("wasm-guests")
    wasm.add_argument("--out", type=Path, required=True)
    wasm.add_argument("--work", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "lockfiles":
        return lockfiles()
    if args.command == "wasm-guests":
        return wasm_guests(args.out, args.work)
    return desktop_status()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
