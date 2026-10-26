#!/usr/bin/env python3
"""Capture one reproducible #3 compilation-boundary measurement run.

This deliberately uses only the Python standard library. Invoke it with
`uv run --no-project`; all Rust work goes through Soldr.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=True)


def version(command: list[str], cwd: Path) -> str:
    return run(command, cwd).stdout.strip()


def cache_snapshot(cwd: Path) -> dict[str, object]:
    completed = subprocess.run(["soldr", "cache", "--json"], cwd=cwd, text=True, capture_output=True, check=False)
    if completed.returncode:
        return {"available": False, "error": completed.stderr.strip()}
    try:
        return {"available": True, "stats": json.loads(completed.stdout)}
    except json.JSONDecodeError:
        return {"available": False, "error": "Soldr did not return JSON", "raw": completed.stdout}


def cache_reuse() -> dict[str, object]:
    """Read the just-finished build's Soldr session before another command replaces it."""
    path = Path.home() / ".soldr" / "cache" / "zccache" / "logs" / "last-session-stats.json"
    try:
        stats = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"available": False, "error": str(error)}
    return {
        "available": True,
        "compilations": stats.get("compilations"),
        "hits": stats.get("hits"),
        "misses": stats.get("misses"),
        "hit_rate": stats.get("hit_rate"),
        "time_saved_ms": stats.get("time_saved_ms"),
    }


def directory_bytes(path: Path) -> int:
    return sum(entry.stat().st_size for entry in path.rglob("*") if entry.is_file())


def timed_build(source: Path, target: Path, features: str, label: str) -> dict[str, object]:
    command = ["soldr", "cargo", "build", "--locked", "--no-default-features", "--timings", "--target-dir", str(target)]
    if features:
        command.extend(("--features", features))
    # Soldr may scrub unknown files from a Cargo target directory. Keep the
    # measurement sidecar just outside it so RSS evidence survives the build.
    time_file = target.parent / f"{target.name}-{label}.time"
    time_command = shutil.which("time")
    if sys.platform.startswith("linux") and time_command:
        wrapped = [time_command, "-f", "%e %M", "-o", str(time_file), *command]
    else:
        wrapped = command
    started = time.monotonic()
    completed = run(wrapped, source)
    elapsed = time.monotonic() - started
    peak_rss_kib: int | None = None
    if time_file.exists():
        fields = time_file.read_text(encoding="utf-8").split()
        if len(fields) == 2:
            elapsed = float(fields[0])
            peak_rss_kib = int(fields[1])
    return {
        "command": command,
        "elapsed_seconds": elapsed,
        # Cargo's wall clock is the build critical path. The corresponding
        # cargo-timings HTML is retained beside the target directory.
        "critical_path_seconds": elapsed,
        "peak_rss_kib": peak_rss_kib,
        "artifact_bytes": directory_bytes(target / "debug") if (target / "debug").exists() else 0,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--features", default="")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    target_root = output / "targets"
    target_root.mkdir(exist_ok=True)
    samples: list[dict[str, object]] = []
    for number in range(1, args.repeat + 1):
        target = target_root / f"{args.label}-{number}"
        if target.exists():
            shutil.rmtree(target)
        before = cache_snapshot(source)
        clean = timed_build(source, target, args.features, f"clean-{number}")
        clean_cache = cache_reuse()
        # A source edit representative of facade implementation work. Preserve
        # contents: Cargo observes the mtime, then the checkout remains clean.
        edit = source / "src" / "lib.rs"
        os.utime(edit, None)
        incremental = timed_build(source, target, args.features, f"incremental-{number}")
        incremental_cache = cache_reuse()
        samples.append(
            {
                "clean": clean,
                "incremental": incremental,
                "cache_before": before,
                "clean_cache": clean_cache,
                "incremental_cache": incremental_cache,
            }
        )
    package = run(["soldr", "cargo", "package", "--locked", "--allow-dirty", "--no-verify"], source)
    crate_files = sorted((source / "target" / "package").glob("kernal-api-*.crate"))
    record = {
        "schema": 1,
        "label": args.label,
        "source": str(source),
        "git_revision": version(["git", "rev-parse", "HEAD"], source),
        "host": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "toolchain": {
            "rustc": version(["soldr", "rustc", "--version"], source),
            "soldr": version(["soldr", "--version"], source),
        },
        "features": args.features or "default (no optional features)",
        "repeat": args.repeat,
        "samples": samples,
        "package_archive_bytes": crate_files[-1].stat().st_size if crate_files else None,
        "package_command_stderr": package.stderr,
    }
    (output / f"{args.label}.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
