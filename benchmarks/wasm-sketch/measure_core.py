#!/usr/bin/env python3
"""Issue #13 core candidate diagnostic; invoke with uv run --no-project.

Retains an isolated committed source snapshot and every command log. It never
edits the user's checkout, deletes an output directory, or executes the guest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import time


def summary(samples: list[int]) -> dict[str, int | float]:
    if len(samples) < 10:
        raise ValueError("at least ten successful edit samples are required")
    ordered = sorted(samples)
    return {"p50_ns": statistics.median(ordered), "p95_ns": ordered[math.ceil(len(ordered) * 0.95) - 1]}


def edit_summary(samples: list[dict[str, object]]) -> dict[str, int | float]:
    for key in ("source_sha256", "module_sha256"):
        values = [sample[key] for sample in samples]
        if len(set(values)) != len(values):
            raise ValueError(f"edit samples repeat {key}; refusing stale-artifact timing")
    return summary([int(sample["wall_ns"]) for sample in samples])


def edit_source(source: str, previous: int, following: int) -> str:
    before = f"kernal_api_v1_bindings::clock_sleep({previous})"
    if source.count(before) != 1:
        raise ValueError("guest clock edit anchor must occur exactly once")
    return source.replace(before, f"kernal_api_v1_bindings::clock_sleep({following})")


def validate_admission(record: object, module_bytes: int) -> dict[str, object]:
    if not isinstance(record, dict):
        raise ValueError("admission measurement must be an object")
    if (
        type(record.get("schema")) is not int
        or record["schema"] != 1
        or record.get("profile") != "threaded-rust-v1"
        or record.get("executed") is not False
        or type(record.get("module_bytes")) is not int
        or record["module_bytes"] != module_bytes
    ):
        raise ValueError("admission measurement does not match the candidate module")
    for name in ("compiler_setup_ns", "admission_ns"):
        if type(record.get(name)) is not int or record[name] < 0:
            raise ValueError(f"invalid admission timing: {name}")
    return record


def build_rss(raw: str) -> int:
    value = raw.strip()
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("GNU time must report one positive peak RSS value in KiB")
    return int(value) * 1024


def run(
    command: list[str], cwd: Path, log: Path, *, gnu_time: Path | None = None, rss_log: Path | None = None
) -> tuple[int, str]:
    if (gnu_time is None) != (rss_log is None):
        raise ValueError("GNU time and its RSS output path must be supplied together")
    if gnu_time is not None:
        command = [str(gnu_time), "-f", "%M", "-o", str(rss_log), "--", *command]
    start = time.monotonic_ns()
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SOLDR_LINKER": "default", "LC_ALL": "C"},
    )
    elapsed = time.monotonic_ns() - start
    log.write_text(result.stdout + "\n--- stderr ---\n" + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}); see {log}")
    return elapsed, result.stdout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--admission", required=True, type=Path)
    parser.add_argument("--embedder", required=True, type=Path)
    parser.add_argument("--edits", default=10, type=int)
    parser.add_argument("--gnu-time", type=Path, help="optional GNU time executable; collect build command peak RSS")
    args = parser.parse_args()
    if args.edits < 10:
        parser.error("--edits must be at least 10")
    repo = Path(__file__).resolve().parents[2]
    admission, embedder = args.admission.resolve(), args.embedder.resolve()
    if not admission.is_file() or not embedder.is_file():
        parser.error("build the admission and metadata executables first")
    gnu_time = args.gnu_time.resolve() if args.gnu_time else None
    time_version = None
    if gnu_time is not None:
        version = subprocess.run(
            [str(gnu_time), "--version"], capture_output=True, text=True, check=True, env={**os.environ, "LC_ALL": "C"}
        )
        if "GNU Time" not in version.stdout:
            parser.error("--gnu-time must name GNU time (not BSD time or a shell builtin)")
        time_version = version.stdout.splitlines()[0]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    archive = output / "source.tar"
    with archive.open("wb") as stream:
        subprocess.run(["git", "archive", revision], cwd=repo, stdout=stream, check=True)
    subprocess.run(["tar", "-xf", str(archive), "-C", str(source)], check=True)
    guest = source / "guests/threaded-smoke"
    guest_source = guest / "src/main.rs"
    target = output / "target"
    artifact = target / "wasm32-wasip1-threads/release/kernal-api-threaded-smoke.wasm"
    admitted = output / "guest.admitted.wasm"
    records: list[dict[str, object]] = []
    document = {
        "schema": 1,
        "status": "incomplete",
        "candidate": "core-threaded-v1",
        "revision": revision,
        "host": platform.platform(),
        "samples": records,
        "admission_sha256": hashlib.sha256(admission.read_bytes()).hexdigest(),
        "embedder_sha256": hashlib.sha256(embedder.read_bytes()).hexdigest(),
        "cache_hit_rate": None,
        "peak_compiler_rss_bytes": None,
        "cache_mode": "soldr-disabled",
        "build_memory": {
            "method": "gnu-time-%M" if gnu_time else None,
            "tool_version": time_version,
            "scope": "build command peak RSS; not aggregate concurrent RSS",
            "peak_rss_bytes": None,
        },
        "limitations": [
            "cache hit rate and compiler-specific RSS are not collected",
            "GNU time does not account for detached daemon processes",
            "no Component Model or execution comparison",
        ],
    }
    previous = 10
    for index in range(args.edits + 2):
        label = "cold" if index == 0 else "noop" if index == 1 else f"edit-{index - 1}"
        if index >= 2:
            following = previous + 1
            guest_source.write_text(
                edit_source(guest_source.read_text(encoding="utf-8"), previous, following), encoding="utf-8"
            )
            previous = following
        started = time.monotonic_ns()
        rss_log = output / f"{label}-build.rss-kib" if gnu_time else None
        build_ns, _ = run(
            [
                "soldr",
                "--no-cache",
                "cargo",
                "build",
                "--locked",
                "--release",
                "--target",
                "wasm32-wasip1-threads",
                "--target-dir",
                str(target),
                "-j",
                "1",
            ],
            guest,
            output / f"{label}-build.log",
            gnu_time=gnu_time,
            rss_log=rss_log,
        )
        peak_rss = build_rss(rss_log.read_text(encoding="utf-8")) if rss_log else None
        shutil.copyfile(artifact, admitted)
        embed_ns, _ = run(
            [str(embedder), "--embed-threaded-metadata", str(admitted)], source, output / f"{label}-metadata.log"
        )
        admit_ns, result = run([str(admission), str(admitted)], source, output / f"{label}-admission.log")
        measurement = validate_admission(json.loads(result), admitted.stat().st_size)
        elapsed = time.monotonic_ns() - started
        records.append(
            {
                "label": label,
                "wall_ns": elapsed,
                "build_ns": build_ns,
                "metadata_ns": embed_ns,
                "admission_command_ns": admit_ns,
                "build_peak_rss_bytes": peak_rss,
                "admission": measurement,
                "source_sha256": hashlib.sha256(guest_source.read_bytes()).hexdigest(),
                "module_sha256": hashlib.sha256(admitted.read_bytes()).hexdigest(),
            }
        )
        (output / "result.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        print(f"{label}: {elapsed / 1_000_000_000:.3f}s", flush=True)
    document["edit_summary"] = edit_summary(records[2:])
    if gnu_time:
        document["build_memory"]["peak_rss_bytes"] = max(sample["build_peak_rss_bytes"] for sample in records)
    document["status"] = "complete-diagnostic-only"
    (output / "result.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
