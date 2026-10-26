"""Package, check and publish the kernal-api crate -- the same steps in CI and locally.

    uv run --no-project ci/crate_release.py package          # build + check the .crate
    uv run --no-project ci/crate_release.py check CRATE      # check an existing .crate
    uv run --no-project ci/crate_release.py publish CRATE --dry-run

``check`` holds the package to source: crates.io's 10 MiB upload cap, and
every file either tracked by git or one of the few Cargo and the release
generate. v0.1.13's crate was 1.28 GB because `cargo package` swept in
setup-soldr's untracked cache archives; crates.io answered 413.

``publish`` uploads only bytes it has checked: it re-packages, requires the
result to match the validated artifact exactly, then runs `cargo publish`.
If crates.io already has the version, it verifies the published checksum
instead. ``--dry-run`` stops at `cargo publish --dry-run`, and needs no token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CRATE = "kernal-api"
# crates.io rejects larger uploads with 413 Payload Too Large.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# Files in the package that git does not track: Cargo writes the first two,
# and the release generates the ConPTY manifest from verified sidecars.
GENERATED = {"Cargo.toml.orig", ".cargo_vcs_info.json", "conpty-sidecar.sha256.toml"}
PACKAGE = ["soldr", "cargo", "package", "--locked", "--all-features", "--allow-dirty"]
PUBLISH = ["soldr", "cargo", "publish", "--locked", "--no-verify", "--allow-dirty"]
USER_AGENT = "kernal-api-release (https://github.com/zackees/kernal-api)"


def version() -> str:
    return tomllib.loads((REPO / "Cargo.toml").read_text(encoding="utf-8"))["package"]["version"]


def packaged_crate(ver: str) -> Path:
    return REPO / "target" / "package" / f"{CRATE}-{ver}.crate"


def run(arguments: list[str]) -> None:
    print("Running: " + " ".join(arguments), flush=True)
    subprocess.run(arguments, cwd=REPO, check=True)


def tracked_files() -> set[str]:
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True, capture_output=True).stdout.decode(
        "utf-8"
    )
    return {path for path in listing.split("\0") if path}


def crate_problems(crate: Path, tracked: set[str], ver: str) -> list[str]:
    """Everything wrong with a .crate; empty when it may be published."""
    problems = []
    size = crate.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        problems.append(f"{crate.name} is {size:,} bytes; crates.io accepts at most {MAX_UPLOAD_BYTES:,}")
    prefix = f"{CRATE}-{ver}/"
    with tarfile.open(crate, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            if not member.name.startswith(prefix):
                problems.append(f"{member.name} is outside {prefix}")
                continue
            relative = member.name[len(prefix) :]
            if relative not in tracked and relative not in GENERATED:
                problems.append(f"{relative} ({member.size:,} bytes) is not tracked by git")
    return problems


def check(crate: Path, ver: str) -> None:
    problems = crate_problems(crate, tracked_files(), ver)
    with tarfile.open(crate, "r:gz") as archive:
        files = sum(1 for member in archive.getmembers() if member.isfile())
    print(f"{crate.name}: {crate.stat().st_size:,} bytes, {files} files", flush=True)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        raise SystemExit(f"{crate.name} is not a publishable source package")


def package(verify: bool) -> Path:
    ver = version()
    run(PACKAGE + ([] if verify else ["--no-verify"]))
    crate = packaged_crate(ver)
    check(crate, ver)
    return crate


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def published_checksum(ver: str) -> str | None:
    """The crates.io checksum of this version, or None when it is unpublished."""
    request = urllib.request.Request(
        f"https://crates.io/api/v1/crates/{CRATE}/{ver}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)["version"]["checksum"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def publish(artifact: Path, dry_run: bool) -> None:
    ver = version()
    check(artifact, ver)
    expected = sha256(artifact)
    remote = published_checksum(ver)
    if remote is not None:
        if remote != expected:
            raise SystemExit(f"crates.io has {CRATE} {ver} with checksum {remote}, not {expected}")
        print(f"crates.io already has {CRATE} {ver} with these exact bytes", flush=True)
        return
    # `cargo publish` packages again; upload only if that is the checked artifact.
    rebuilt = package(verify=False)
    if sha256(rebuilt) != expected:
        raise SystemExit(f"re-packaged {rebuilt.name} differs from the validated artifact {artifact}")
    run(PUBLISH + (["--dry-run"] if dry_run else []))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    package_command = commands.add_parser("package", help="build the .crate and check it")
    package_command.add_argument("--no-verify", action="store_true", help="skip cargo's build of the package")
    check_command = commands.add_parser("check", help="check an existing .crate")
    check_command.add_argument("crate", type=Path)
    publish_command = commands.add_parser("publish", help="publish a validated .crate, or verify it is published")
    publish_command.add_argument("crate", type=Path)
    publish_command.add_argument("--dry-run", action="store_true", help="stop at cargo publish --dry-run")
    arguments = parser.parse_args(argv)
    if arguments.command == "package":
        package(verify=not arguments.no_verify)
    elif arguments.command == "check":
        check(arguments.crate.resolve(), version())
    else:
        publish(arguments.crate.resolve(), arguments.dry_run)


if __name__ == "__main__":
    main()
