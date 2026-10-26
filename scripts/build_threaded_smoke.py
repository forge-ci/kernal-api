#!/usr/bin/env python3
"""Build the threaded-smoke Wasm guest and run the real-artifact proofs.

Usage: ``python scripts/build_threaded_smoke.py [<admitted-artifact.wasm>]``

Without an artifact, builds one into ``$CARGO_TARGET_DIR/kernal-api-threaded-smoke``
(or a temporary directory when ``CARGO_TARGET_DIR`` is unset), keeping the guest
build on Soldr's front door, cache included -- never ambient Cargo.
ci/rust_target.py checks both core and std archives and repairs stale rustup
target bookkeeping before Cargo sees it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUEST = REPO / "guests" / "threaded-smoke"
TARGET = "wasm32-wasip1-threads"
PARENT_DEATH_TEST = "failure_proof::d4_parent_death_kills_exact_worker"
ONE_PASSED = "test result: ok. 1 passed; 0 failed; 0 ignored;"


class SmokeError(RuntimeError):
    """A build step or proof failed; the message says which."""


@dataclass(frozen=True)
class Output:
    returncode: int
    lines: tuple[str, ...]


def run(arguments: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    returncode = subprocess.run(arguments, cwd=cwd, env=env, check=False).returncode
    if returncode != 0:
        raise SmokeError(f"{' '.join(arguments)} exited {returncode}")


def stream(arguments: list[str], *, env: dict[str, str] | None = None) -> Output:
    """Run a command, echoing its combined output live, and keep the lines."""
    with subprocess.Popen(
        arguments, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
    ) as process:
        assert process.stdout is not None
        lines = []
        for line in process.stdout:
            sys.stdout.write(line)
            lines.append(line.rstrip("\r\n"))
    return Output(process.returncode, tuple(lines))


def build(target_directory: Path) -> Path:
    run([sys.executable, str(REPO / "ci" / "rust_target.py"), TARGET])
    run(
        [
            "soldr",
            "cargo",
            "build",
            "--locked",
            "--manifest-path",
            "Cargo.toml",
            "--target",
            TARGET,
            "--release",
            "--target-dir",
            str(target_directory),
        ],
        cwd=GUEST,
        env=dict(os.environ, SOLDR_LINKER="default"),
    )
    release = target_directory / TARGET / "release"
    artifact = release / "kernal-api-threaded-smoke.admitted.wasm"
    # Keep Cargo's output pristine: changing the generated ABI contract must
    # not require recompiling an otherwise unchanged guest to replace metadata.
    shutil.copyfile(release / "kernal-api-threaded-smoke.wasm", artifact)
    run(
        [
            "soldr",
            "cargo",
            "run",
            "--locked",
            "--manifest-path",
            str(REPO / "tools" / "wasm-abi-generator" / "Cargo.toml"),
            "--",
            "--embed-threaded-metadata",
            str(artifact),
        ]
    )
    return artifact


def prove(artifact: Path) -> None:
    env = dict(os.environ, KERNAL_API_THREADED_ARTIFACT_WASM=str(artifact))
    test = ["soldr", "cargo", "test", "--locked"]
    run(
        [
            *test,
            "--features",
            "wasm-sketch-host",
            "--lib",
            "supplied_threaded_artifact_admits_and_executes_the_public_profile",
        ],
        env=env,
    )
    run(
        [
            *test,
            "--features",
            "wasm-sketch-worker",
            "--test",
            "wasm_worker_containment",
            "cargo_built_threaded_guest_",
            "--",
            "--ignored",
            "--test-threads=1",
        ],
        env=env,
    )
    support = [*test, "--features", "wasm-sketch-worker-test-support", "--test", "wasm_worker_containment"]
    run([*support, "cargo_built_threaded_guest_forced_output_cleanup", "--", "--ignored"], env=env)
    # An external process-lifecycle proof, not an in-process cancellation
    # assertion: it records real parent-death evidence on every host. Cargo
    # treats an empty test filter as success, so first verify this exact
    # proof is registered.
    listed = stream([*support, "--", "--list"], env=env)
    if listed.returncode != 0:
        raise SmokeError(f"listing the containment tests exited {listed.returncode}")
    if listed.lines.count(f"{PARENT_DEATH_TEST}: test") != 1:
        raise SmokeError("expected exactly one registered parent-death containment test")
    result = stream([*support, PARENT_DEATH_TEST, "--", "--exact", "--test-threads=1"], env=env)
    if result.returncode != 0:
        raise SmokeError(f"parent-death containment exited {result.returncode}")
    if sum(ONE_PASSED in line for line in result.lines) != 1:
        raise SmokeError("parent-death containment did not run exactly one non-ignored test")


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print("usage: build_threaded_smoke.py [<admitted-artifact.wasm>]", file=sys.stderr)
        return 2
    try:
        if argv:
            prove(Path(argv[0]))
            return 0
        root = os.environ.get("CARGO_TARGET_DIR")
        if root:
            prove(build(Path(root) / "kernal-api-threaded-smoke"))
            return 0
        with tempfile.TemporaryDirectory(prefix="kernal-api-threaded-smoke-") as temporary:
            prove(build(Path(temporary) / "target"))
    except SmokeError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
