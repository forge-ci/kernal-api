#!/usr/bin/env python3
"""Block release packaging until `running-process` has its released source.

Run with:

    uv run --no-project --with tomli==2.2.1 python ci/check_release_process_substrate.py

Python 3.11+ supplies :mod:`tomllib`; Python 3.10 uses the explicit ``tomli``
dependency supplied by the command above. The check deliberately reads only
tracked Cargo manifests, so a release checkout cannot accidentally rely on a
developer's sister checkout or silently substitute a registry crate.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Sequence, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


Issue = Tuple[str, str]
DEPENDENCY_TABLES = ("dependencies", "build-dependencies", "dev-dependencies")
REQUIRED_RUNNING_PROCESS_VERSION = "=4.10.20"


def is_running_process_name(name: str) -> bool:
    """Whether a Cargo key names the package or a `replace` package id."""

    return name == "running-process" or name.startswith("running-process:")


def is_running_process_dependency(name: str, value: Any) -> bool:
    """Identify direct and renamed dependency entries for the substrate."""

    return is_running_process_name(name) or (isinstance(value, Mapping) and value.get("package") == "running-process")


def dependency_release_violation(name: str, value: Any, context: str, *, replacement: bool) -> str | None:
    """Return one release-policy violation for a process-substrate binding."""

    if not is_running_process_dependency(name, value):
        return None

    location = f"{context}.{name}" if context else name
    if replacement:
        return f"{location}: running-process patch/replacement is forbidden"
    if isinstance(value, Mapping) and value.get("workspace") is True:
        # Member manifests inherit their source/version from the workspace
        # table, which is inspected independently.
        return None
    if isinstance(value, Mapping) and "path" in value:
        return f"{location}: local path for running-process"
    if isinstance(value, Mapping) and "git" in value:
        return f"{location}: non-registry Git source for running-process"

    version = value.get("version") if isinstance(value, Mapping) else value
    if version != REQUIRED_RUNNING_PROCESS_VERSION:
        return f"{location}: running-process must use exact registry version {REQUIRED_RUNNING_PROCESS_VERSION}"
    return None


def dependency_table_release_violations(table: Mapping[str, Any], context: str, *, replacement: bool) -> List[str]:
    """Return process-substrate release violations from one Cargo table."""

    findings: List[str] = []
    for key, value in table.items():
        violation = dependency_release_violation(str(key), value, context, replacement=replacement)
        if violation is not None:
            findings.append(violation)
    return findings


def dependency_tables_at(manifest: Mapping[str, Any], context: str) -> List[Tuple[Mapping[str, Any], str]]:
    """Collect normal, build, and dev dependency tables at a Cargo scope."""

    tables: List[Tuple[Mapping[str, Any], str]] = []
    for table_name in DEPENDENCY_TABLES:
        table = manifest.get(table_name)
        if isinstance(table, Mapping):
            location = f"{context}.{table_name}" if context else table_name
            tables.append((table, location))
    return tables


def running_process_release_violations(manifest: Mapping[str, Any]) -> List[str]:  # noqa: C901 -- Existing release bindings scan; ratchet complexity 12.
    """Return every non-release-ready `running-process` manifest binding."""

    tables: List[Tuple[Mapping[str, Any], str, bool]] = []
    for scope_name, scope in manifest.items():
        if scope_name in DEPENDENCY_TABLES and isinstance(scope, Mapping):
            tables.append((scope, str(scope_name), False))
        elif scope_name == "workspace" and isinstance(scope, Mapping):
            tables.extend((table, context, False) for table, context in dependency_tables_at(scope, "workspace"))
        elif scope_name == "target" and isinstance(scope, Mapping):
            for target_name, target in scope.items():
                if isinstance(target, Mapping):
                    tables.extend(
                        (table, context, False)
                        for table, context in dependency_tables_at(target, f"target.{target_name}")
                    )
        elif scope_name == "patch" and isinstance(scope, Mapping):
            for source, patch_table in scope.items():
                if isinstance(patch_table, Mapping):
                    tables.append((patch_table, f"patch.{source}", True))
        elif scope_name == "replace" and isinstance(scope, Mapping):
            tables.append((scope, "replace", True))

    findings: List[str] = []
    for table, context, replacement in tables:
        findings.extend(dependency_table_release_violations(table, context, replacement=replacement))
    return findings


def load_manifest(path: Path) -> Mapping[str, Any]:
    with path.open("rb") as handle:
        parsed = tomllib.load(handle)
    if not isinstance(parsed, Mapping):
        raise ValueError(f"{path}: Cargo manifest root is not a TOML table")
    return parsed


def tracked_cargo_manifests(repository: Path) -> List[Path]:
    """Return all tracked Cargo manifests without walking untracked content."""

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "ls-files",
            "-z",
            "--",
            "Cargo.toml",
            ":(glob)**/Cargo.toml",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"cannot list tracked Cargo manifests: {detail}")

    return [repository / Path(raw.decode()) for raw in result.stdout.split(b"\0") if raw]


def release_blockers(manifests: Iterable[Path]) -> List[Issue]:
    """Report every tracked manifest that cannot use the released substrate."""

    blockers: List[Issue] = []
    for manifest_path in manifests:
        for violation in running_process_release_violations(load_manifest(manifest_path)):
            blockers.append((str(manifest_path), violation))
    return blockers


def parse_args(arguments: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository whose tracked Cargo.toml files are checked",
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if arguments is None else arguments)
    repository = args.repo.resolve()
    try:
        blockers = release_blockers(tracked_cargo_manifests(repository))
    except (OSError, RuntimeError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"release guard could not inspect Cargo manifests: {error}", file=sys.stderr)
        return 2

    if not blockers:
        return 0

    print("release blocked: running-process is not the exact released registry dependency", file=sys.stderr)
    for manifest, violation in blockers:
        print(f"  {manifest}: {violation}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
