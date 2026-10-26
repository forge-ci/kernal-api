#!/usr/bin/env python3
"""Verify built ConPTY sidecar archives against their sha256 manifest.

Usage: ``python ci/verify_conpty_assets.py <asset-directory>``
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ARCHES = ("x64", "arm64", "x86")
ENTRIES = ("conpty.dll", "OpenConsole.exe", "VERSION.txt")


class VerifyError(RuntimeError):
    """The assets do not match the manifest."""


@dataclass(frozen=True)
class Expected:
    sha256: str
    size_bytes: int


def expected_for(manifest: Path, arch: str) -> Expected:
    document = tomllib.loads(manifest.read_text(encoding="utf-8"))
    table = document.get("asset", {}).get(arch, {})
    return Expected(sha256=str(table.get("sha256", "")), size_bytes=int(table.get("size_bytes", -1)))


def archive_entries(archive: Path) -> list[str]:
    with subprocess.Popen(["tar", "--zstd", "-tf", str(archive)], stdout=subprocess.PIPE, text=True) as process:
        assert process.stdout is not None
        entries = [line.rstrip("\n") for line in process.stdout]
    if process.returncode != 0:
        raise VerifyError(f"cannot list {archive}")
    return entries


def verify(asset_dir: Path) -> None:
    manifest = asset_dir / "conpty-sidecar.sha256.toml"
    if not manifest.is_file():
        raise VerifyError(f"ConPTY asset manifest is missing: {manifest}")
    for arch in ARCHES:
        archive = asset_dir / f"conpty-sidecar-{arch}.tar.zst"
        if not archive.is_file() or archive.stat().st_size == 0:
            raise VerifyError(f"ConPTY archive is missing or empty: {archive}")
        expected = expected_for(manifest, arch)
        data = archive.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected.sha256 or len(data) != expected.size_bytes:
            raise VerifyError(f"ConPTY manifest does not match {archive}")
        entries = archive_entries(archive)
        for entry in ENTRIES:
            pattern = re.compile(rf"^(\./)?{re.escape(entry)}$")
            if not any(pattern.match(line) for line in entries):
                raise VerifyError(f"ConPTY archive {archive} is missing {entry}")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: verify_conpty_assets.py <asset-directory>", file=sys.stderr)
        return 2
    try:
        verify(Path(argv[0]))
    except VerifyError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
