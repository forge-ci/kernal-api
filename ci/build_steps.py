#!/usr/bin/env python3
"""The multi-command steps of ci.yml's `build` job (GEN-005).

``archive --target T [--cross C]``
    Archive every test binary for one target. A native lane (`--cross` empty)
    builds for the host WITHOUT `--target`: naming the triple makes Soldr link
    through a zig shim the runner does not carry. Windows keeps debug info in
    a `.pdb` beside the `.exe`, and nextest archives the executable without
    it; `kernal-symbolize`'s tests read their own binary's PDB.
    `archive.include` takes paths, not globs, and the name carries a build
    hash, so build first, name the PDB, archive with it. Only that one:
    `deps` is half a gigabyte for one binary.

``embed-resources --target T``
    `build-resources` as it is meant to run: in an application's build script
    on this (Linux) host, compiling resources for the Windows target.
    embed-resource invokes `llvm-rc` for an MSVC target and reads the
    documented `RC_<target>` override, which is how a host that carries only
    versioned LLVM binaries names its own. Resolve one rather than assume the
    plain name is on PATH; failing to compile resources stays a real failure.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import sys
from pathlib import Path

try:
    from ci import gha
except ImportError:  # run as a script: ci/ is sys.path[0]
    import gha


def features() -> str:
    value = os.environ.get("KERNAL_TARGET_FEATURES", "")
    if not value:
        raise gha.StepError("KERNAL_TARGET_FEATURES is not set; run `ci/target_features.py --github-env` first")
    return value


def archive_config(target: str) -> Path:
    """A nextest config that ships the kernal-symbolize PDB in the archive."""
    pdbs = sorted(glob.glob(f"target/{target}/debug/deps/kernal_symbolize-*.pdb"))
    if not pdbs:
        raise gha.StepError("the kernal-symbolize test build produced no PDB")
    config = gha.runner_temp() / "nextest-archive.toml"
    shutil.copyfile(".config/nextest.toml", config)
    entries = "".join(
        f'  {{ path = "{pdb.removeprefix("target/")}", relative-to = "target", on-missing = "error" }},\n'
        for pdb in pdbs
    )
    with config.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[profile.default.archive]\ninclude = [\n{entries}]\n")
    return config


def archive(target: str, cross: str) -> int:
    target_args = ["--target", cross] if cross else []
    config_args: list[str] = []
    if target.endswith("-pc-windows-msvc"):
        gha.run(["soldr", "cargo", "test", "--locked", "--features", features(), *target_args, "--no-run"])
        config_args = ["--config-file", str(archive_config(target))]
    gha.run(
        [
            "soldr",
            "cargo",
            "nextest",
            "archive",
            "--locked",
            "--features",
            features(),
            *target_args,
            *config_args,
            "--archive-file",
            f"kernal-tests-{target}.tar.zst",
        ]
    )
    return 0


def version_key(path: str) -> list[int]:
    return [int(number) for number in re.findall(r"\d+", path)]


def find_rc() -> str:
    found = shutil.which("llvm-rc")
    if found:
        return found
    candidates = glob.glob("/usr/lib/llvm-*/bin/llvm-rc") + glob.glob("/usr/bin/llvm-rc-*")
    return max(candidates, key=version_key) if candidates else ""


def embed_resources(target: str) -> int:
    rc = find_rc()
    if not rc:
        gha.run(["sudo", "apt-get", "update"])
        gha.run(["sudo", "apt-get", "install", "--yes", "llvm"])
        rc = find_rc()
    if not rc:
        raise gha.StepError(f"no llvm-rc on this host; embed-resource cannot target {target}")
    print(f"resource compiler: {rc}")
    env = dict(os.environ, **{f"RC_{target.replace('-', '_')}": rc})
    gha.run(
        [
            "soldr",
            "cargo",
            "check",
            "--locked",
            "--manifest-path",
            "tests/build-resources-consumer/Cargo.toml",
            "--target",
            target,
        ],
        env=env,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ci.yml build job steps")
    sub = parser.add_subparsers(dest="command", required=True)
    archive_parser = sub.add_parser("archive")
    archive_parser.add_argument("--target", required=True)
    archive_parser.add_argument("--cross", default="")
    sub.add_parser("embed-resources").add_argument("--target", required=True)
    args = parser.parse_args(sys.argv[1:])
    if args.command == "archive":
        return archive(args.target, args.cross)
    return embed_resources(args.target)


if __name__ == "__main__":
    gha.main_guard(main)
