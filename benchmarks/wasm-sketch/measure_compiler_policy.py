#!/usr/bin/env python3
"""Matched #13 compiler-policy candidate diagnostic; invoke with uv run --no-project.

Both candidates are built from one archived revision and every edit changes the
same request-key input in shared/compiler_policy.rs.  This runner retains
artifacts and command logs, never edits the user's checkout, and does not
choose a runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import time
from pathlib import Path

from measure_core import build_rss, edit_summary, run, validate_admission
from measure_component import validate_output


def edit_source(source: str, previous: int, following: int) -> str:
    before_name = "source.c" if previous == 0 else f"source-{previous}.c"
    after_name = f"source-{following}.c"
    before = f'let raw = ["-MD", "-MF-", "{before_name}"].map(String::from);'
    after = f'let raw = ["-MD", "-MF-", "{after_name}"].map(String::from);'
    if source.count(before) != 1:
        raise ValueError("shared request-key edit anchor must occur exactly once")
    return source.replace(before, after)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(repo: Path, output: Path) -> tuple[Path, str]:
    source = output / "source"
    source.mkdir()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    archive = output / "source.tar"
    with archive.open("wb") as stream:
        subprocess.run(["git", "archive", revision], cwd=repo, stdout=stream, check=True)
    subprocess.run(["tar", "-xf", str(archive), "-C", str(source)], check=True)
    return source, revision


def command_memory(path: Path | None) -> int | None:
    return build_rss(path.read_text(encoding="utf-8")) if path else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--admission", required=True, type=Path)
    parser.add_argument("--embedder", required=True, type=Path)
    parser.add_argument("--encoder", required=True, type=Path)
    parser.add_argument("--edits", default=10, type=int)
    parser.add_argument("--gnu-time", type=Path)
    args = parser.parse_args()
    if args.edits < 10:
        parser.error("--edits must be at least 10")
    helpers = {
        name: path.resolve()
        for name, path in {
            "admission": args.admission,
            "embedder": args.embedder,
            "encoder": args.encoder,
        }.items()
    }
    if any(not path.is_file() for path in helpers.values()):
        parser.error("build the admission, metadata, and engine-probe encoder executables first")
    gnu_time = args.gnu_time.resolve() if args.gnu_time else None
    time_version = None
    if gnu_time:
        version = subprocess.run(
            [str(gnu_time), "--version"], capture_output=True, text=True, check=True, env={"LC_ALL": "C"}
        )
        if "GNU Time" not in version.stdout:
            parser.error("--gnu-time must name GNU time (not BSD time or a shell builtin)")
        time_version = version.stdout.splitlines()[0]

    repo = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source, revision = snapshot(repo, output)
    shared = source / "benchmarks/wasm-sketch/shared/compiler_policy.rs"
    target = output / "target"
    core_guest = source / "benchmarks/wasm-sketch/compiler-guest"
    component_guest = source / "benchmarks/wasm-sketch/component-guest"
    records: list[dict[str, object]] = []
    document: dict[str, object] = {
        "schema": 1,
        "status": "incomplete",
        "candidate_pair": "core-compiler-v1/component-compiler-private",
        "revision": revision,
        "host": platform.platform(),
        "shared_edit": "compiler_policy.rs request-key source filename",
        "samples": records,
        "helper_sha256": {name: sha256(path) for name, path in helpers.items()},
        "cache_mode": "soldr-disabled",
        "cache_hit_rate": None,
        "peak_compiler_rss_bytes": None,
        "build_memory": {
            "method": "gnu-time-%M" if gnu_time else None,
            "tool_version": time_version,
            "scope": "each build command high-water mark; not aggregate process RSS",
            "peak_rss_bytes": None,
        },
        "limitations": [
            "candidate adapters differ: Core metadata admission versus Component encoding and engine compilation",
            "the Component candidate lacks Core exact-output artifact authority",
            "this validates artifacts but does not execute every timed edit",
            "Soldr cache-hit rate and compiler-specific/process-tree RSS are not collected",
            "GNU time excludes detached daemon processes",
            "no six-native-host acceptance or runtime selection is implied",
        ],
    }

    previous = 0
    for index in range(args.edits + 2):
        label = "cold" if index == 0 else "noop" if index == 1 else f"edit-{index - 1}"
        if index >= 2:
            following = previous + 1
            shared.write_text(edit_source(shared.read_text(encoding="utf-8"), previous, following), encoding="utf-8")
            previous = following
        source_digest = sha256(shared)

        core_artifact = target / "core/wasm32-wasip1-threads/release/kernal-compiler-guest-proof.wasm"
        core_admitted = output / f"{label}.core.admitted.wasm"
        core_rss = output / f"{label}.core-build.rss-kib" if gnu_time else None
        core_start = time.monotonic_ns()
        core_build_ns, _ = run(
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
                str(target / "core"),
                "--features",
                "guest-proof",
                "--bin",
                "kernal-compiler-guest-proof",
                "-j",
                "1",
            ],
            core_guest,
            output / f"{label}.core-build.log",
            gnu_time=gnu_time,
            rss_log=core_rss,
        )
        shutil.copyfile(core_artifact, core_admitted)
        metadata_ns, _ = run(
            [str(helpers["embedder"]), "--embed-threaded-metadata", str(core_admitted)],
            source,
            output / f"{label}.core-metadata.log",
        )
        admission_ns, admission_output = run(
            [str(helpers["admission"]), str(core_admitted)], source, output / f"{label}.core-admission.log"
        )
        admission = validate_admission(json.loads(admission_output), core_admitted.stat().st_size)
        core_wall_ns = time.monotonic_ns() - core_start

        component_artifact = target / "component/wasm32-unknown-unknown/release/kernal_component_probe.wasm"
        component = output / f"{label}.component.wasm"
        component_rss = output / f"{label}.component-build.rss-kib" if gnu_time else None
        component_start = time.monotonic_ns()
        component_build_ns, _ = run(
            [
                "soldr",
                "--no-cache",
                "cargo",
                "build",
                "--locked",
                "--release",
                "--target",
                "wasm32-unknown-unknown",
                "--target-dir",
                str(target / "component"),
                "--features",
                "compiler-proof",
                "-j",
                "1",
            ],
            component_guest,
            output / f"{label}.component-build.log",
            gnu_time=gnu_time,
            rss_log=component_rss,
        )
        encoding_ns, encoding_output = run(
            [str(helpers["encoder"]), str(component_artifact), str(component)],
            source,
            output / f"{label}.component-encode.log",
        )
        validate_output(encoding_output, component.stat().st_size)
        component_wall_ns = time.monotonic_ns() - component_start
        records.append(
            {
                "label": label,
                "source_sha256": source_digest,
                "core": {
                    "wall_ns": core_wall_ns,
                    "build_ns": core_build_ns,
                    "metadata_ns": metadata_ns,
                    "admission_command_ns": admission_ns,
                    "build_peak_rss_bytes": command_memory(core_rss),
                    "module_sha256": sha256(core_admitted),
                    "module_bytes": core_admitted.stat().st_size,
                    "admission": admission,
                },
                "component": {
                    "wall_ns": component_wall_ns,
                    "build_ns": component_build_ns,
                    "encode_and_compile_command_ns": encoding_ns,
                    "build_peak_rss_bytes": command_memory(component_rss),
                    "module_sha256": sha256(component),
                    "module_bytes": component.stat().st_size,
                },
            }
        )
        (output / "result.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        print(f"{label}: core {core_wall_ns / 1e9:.3f}s; component {component_wall_ns / 1e9:.3f}s", flush=True)

    edits = records[2:]
    for candidate in ("core", "component"):
        candidate_samples = [sample[candidate] for sample in edits]
        module_hashes = [sample["module_sha256"] for sample in candidate_samples]
        if len(set(module_hashes)) != len(module_hashes):
            raise ValueError(f"{candidate} edit samples repeat a module hash")
        document[f"{candidate}_edit_summary"] = edit_summary(
            [
                {
                    "wall_ns": sample["wall_ns"],
                    "source_sha256": record["source_sha256"],
                    "module_sha256": sample["module_sha256"],
                }
                for record, sample in zip(edits, candidate_samples)
            ]
        )
    if gnu_time:
        document["build_memory"]["peak_rss_bytes"] = max(
            sample[candidate]["build_peak_rss_bytes"] for sample in records for candidate in ("core", "component")
        )
    document["status"] = "complete-matched-diagnostic-only"
    (output / "result.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
