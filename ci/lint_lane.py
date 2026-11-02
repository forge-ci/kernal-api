"""RUST-033 lint lane: the remote lint phase, every target it covers, locally.

Mirrors CI exactly rather than being stricter than it:

* host (x86_64 Linux): ``soldr lint rust --host-only --all-features`` --
  formatting, Clippy with ``--all-targets -D warnings`` and the host Dylint
  pass, the scope of ci.yml's ``linux`` "Lint every target" and ``dylints``.
* each Linux-hosted cross target of ci.yml's ``build`` matrix:
  ``soldr prepare --target <t>`` (what setup-soldr's ``cross-targets`` does)
  then ``soldr cargo clippy --locked --features <target graph> --all-targets
  --target <t> -- --deny warnings`` with ``ci/target_features.py``'s graph,
  the same flags as that job's "Lint this target" step.

``aarch64-unknown-linux-gnu`` is linted natively on an ARM runner in CI
(WebKitGTK needs the distribution's own ABI); a Linux x64 host has no target
pkg-config sysroot for it, so this lane does not lint or attest it
(forge-ci/kernal-api#516).

Run through ``ci/local_gate.py --job lint``; processes go through
running-process, never ``subprocess``.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST_LINT = ("soldr", "lint", "rust", "--host-only", "--all-features")
CROSS_TARGETS = (
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
)


def target_features() -> str:
    sys.path.insert(0, str(ROOT / "ci"))
    from target_features import target_features as features  # pylint: disable=import-outside-toplevel

    return ",".join(features())


def cross_commands(target: str, features: str) -> list[list[str]]:
    return [
        ["soldr", "prepare", "--target", target],
        [
            "soldr",
            "cargo",
            "clippy",
            "--locked",
            "--features",
            features,
            "--all-targets",
            "--target",
            target,
            "--",
            "--deny",
            "warnings",
        ],
    ]


def commands(features: str) -> list[list[str]]:
    plan = [list(HOST_LINT)]
    for target in CROSS_TARGETS:
        plan.extend(cross_commands(target, features))
    return plan


def main() -> None:
    # Imported here so the CI guard suite can import this module without it; the
    # lane installs it (uv --with running-process), the Pylint job does not.
    from running_process import RunningProcess  # pylint: disable=import-outside-toplevel,import-error

    for argv in commands(target_features()):
        print(f"lint lane: {' '.join(argv)}", flush=True)
        RunningProcess.run(argv, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
