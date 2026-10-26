#!/usr/bin/env python3
"""Stage the files the macOS Recovery guest fetches from http://10.0.2.2:8000/.

The action serves a directory from the Linux runner, so everything the guest
needs must be a regular file here: the cross-built nextest archive, a
*Mach-O* cargo-nextest, and the optional extra filter expression.

The archive is built by `soldr cargo nextest archive` on this Linux host,
which means the guest's cargo-nextest must be a macOS binary -- not the one
on PATH. nextest publishes a universal (x86_64 + arm64) macOS build for every
release, so the guest binary is pinned to the exact version that produced the
archive and verified against nextest's published sha256 (the `sha256sum
--check` contract, done here in Python).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

RELEASE_BASE = "https://github.com/nextest-rs/nextest/releases/download"
# Fat (universal) and thin Mach-O magics, in either byte order.
MACHO_MAGICS = frozenset({"cafebabe", "bebafeca", "cffaedfe", "cefaedfe", "feedfacf", "feedface"})


class StageError(RuntimeError):
    """Staging cannot produce a guest share the guest could trust."""


@dataclass(frozen=True)
class Release:
    """nextest's release assets for one version."""

    version: str

    @property
    def stem(self) -> str:
        return f"cargo-nextest-{self.version}-universal-apple-darwin"

    @property
    def tarball(self) -> str:
        return f"{self.stem}.tar.gz"

    @property
    def url(self) -> str:
        return f"{RELEASE_BASE}/cargo-nextest-{self.version}/{self.tarball}"

    @property
    def checksum_url(self) -> str:
        # nextest names the checksum asset after the *stem*, not the tarball,
        # so the two names are built separately rather than by appending
        # `.sha256` to the tarball URL.
        return f"{RELEASE_BASE}/cargo-nextest-{self.version}/{self.stem}.sha256"


def nextest_version() -> str:
    """The version of the nextest that built the archive (its format owner)."""
    if shutil.which("cargo-nextest"):
        command = ["cargo-nextest", "nextest", "--version"]
    else:
        command = ["soldr", "cargo", "nextest", "--version"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        assert process.stdout is not None
        lines = list(process.stdout)
    for line in lines:
        if line.startswith("release: "):
            return line.removeprefix("release: ").strip()
    return ""


def download(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out)


def verify_checksum(tarball: Path, checksum_file: Path) -> None:
    fields = checksum_file.read_text(encoding="utf-8").split()
    if len(fields) != 2 or fields[1].lstrip("*") != tarball.name:
        raise StageError(f"{checksum_file.name} is not a strict sha256sum line for {tarball.name}")
    actual = hashlib.sha256(tarball.read_bytes()).hexdigest()
    if actual != fields[0].lower():
        raise StageError(f"{tarball.name}: sha256 mismatch ({actual} != {fields[0]})")
    print(f"{tarball.name}: OK")


def macho_magic(path: Path) -> str:
    with path.open("rb") as handle:
        return handle.read(4).hex()


def stage(archive: Path, share: Path, release: Release, nextest_filter: str) -> None:
    print(f"staging guest share in {share}")
    print(f"  archive:  {archive}")
    print(f"  nextest:  {release.version} (universal-apple-darwin)")
    shutil.rmtree(share, ignore_errors=True)
    share.mkdir(parents=True)
    shutil.copyfile(archive, share / "kernal-x64.tar.zst")
    # Download the Mach-O nextest and verify it before it can reach the guest.
    # The guest has no way to check provenance, so this is the only place it happens.
    with tempfile.TemporaryDirectory() as temporary:
        tmp = Path(temporary)
        tarball = tmp / release.tarball
        download(release.url, tarball)
        checksum = tmp / f"{release.tarball}.sha256"
        download(release.checksum_url, checksum)
        verify_checksum(tarball, checksum)
        with tarfile.open(tarball, "r:gz") as bundle:
            bundle.extractall(tmp, filter="data")
        binary = tmp / "cargo-nextest"
        if not binary.is_file():
            raise StageError(f"{release.tarball} did not contain a cargo-nextest binary")
        # The guest runs on Intel; confirm the payload is really a macOS
        # universal binary rather than a same-named Linux artifact.
        magic = macho_magic(binary)
        if magic not in MACHO_MAGICS:
            raise StageError(f"staged cargo-nextest is not a Mach-O binary (magic {magic})")
        print(f"  verified Mach-O payload (magic {magic})")
        shutil.copyfile(binary, share / "cargo-nextest")
        (share / "cargo-nextest").chmod(0o755)
    # Optional operator filter from the workflow_dispatch input. Always
    # written, so the guest script can fetch it unconditionally.
    (share / "filter.txt").write_text(f"{nextest_filter}\n", encoding="utf-8")
    (share / "filter.txt").chmod(0o644)
    for entry in sorted(share.iterdir()):
        print(f"  {entry.stat().st_size:>12}  {entry.name}")


def main() -> int:
    archive = Path(os.environ.get("ARCHIVE") or Path.cwd() / "kernal-x64.tar.zst")
    share = Path(os.environ.get("SHARE") or Path.cwd() / "share")
    if not archive.is_file():
        print(f"missing nextest archive {archive} -- run ci/macos-x64/build_archive.py first", file=sys.stderr)
        return 1
    version = nextest_version()
    if not version:
        print("could not determine the cargo-nextest version that built the archive", file=sys.stderr)
        return 1
    try:
        stage(archive, share, Release(version), os.environ.get("NEXTEST_FILTER", ""))
    except StageError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
