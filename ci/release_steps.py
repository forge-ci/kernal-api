#!/usr/bin/env python3
"""The multi-command steps of release.yml (GEN-005).

``verify-version --tag vX.Y.Z``   Cargo.toml, pyproject.toml and the Python
                                  package all carry exactly the tagged version.
``conpty``                        build, verify and stage the ConPTY sidecars.
``stage-registry``                flat registry-packages/ (crate + dist).
``stage-worker --target T [--extension .exe]``
``publish-pypi --tag vX.Y.Z``     upload, or verify byte-identical artifacts.
``release-assets``                create the GitHub release, or verify that an
                                  existing published one carries identical
                                  assets (manual recovery never overwrites).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

try:
    from ci import gha
except ImportError:  # run as a script: ci/ is sys.path[0]
    import gha

CONPTY_DIR = "target/conpty-assets"
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def manifest_version() -> str:
    match = re.search(r'(?m)^version = "([^"]*)"', Path("Cargo.toml").read_text(encoding="utf-8"))
    return match.group(1) if match else ""


def verify_version(tag: str) -> int:
    version = manifest_version()
    if f"v{version}" != tag:
        raise gha.StepError(f"Cargo.toml version {version!r} is not tag {tag}")
    if version == "0.0.0":
        raise gha.StepError("Cargo.toml still carries the 0.0.0 placeholder version")
    if f'version = "{version}"' not in Path("pyproject.toml").read_text(encoding="utf-8"):
        raise gha.StepError(f"pyproject.toml does not carry version {version}")
    if f'__version__ = "{version}"' not in Path("python/kernal_api/__init__.py").read_text(encoding="utf-8"):
        raise gha.StepError(f"python/kernal_api/__init__.py does not carry version {version}")
    return 0


def conpty() -> int:
    gha.run([sys.executable, "ci/build_conpty_assets.py", CONPTY_DIR])
    gha.run([sys.executable, "ci/verify_conpty_assets.py", CONPTY_DIR])
    shutil.copyfile(f"{CONPTY_DIR}/conpty-sidecar.sha256.toml", "conpty-sidecar.sha256.toml")
    print(f"cp {CONPTY_DIR}/conpty-sidecar.sha256.toml conpty-sidecar.sha256.toml")
    return 0


def stage_registry() -> int:
    out = Path("registry-packages")
    out.mkdir()
    files = sorted(glob.glob("target/package/kernal-api-*.crate")) + sorted(glob.glob("dist/*"))
    if not any(name.endswith(".crate") for name in files) or not any(name.startswith("dist") for name in files):
        raise gha.StepError("expected a packaged .crate and Python dist artifacts")
    for name in files:
        shutil.copy2(name, out)
    return 0


def stage_worker(target: str, extension: str) -> int:
    out = Path("release-worker")
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        Path("target") / target / "release" / f"kernal-symbolize{extension}",
        out / f"kernal-symbolize-{target}{extension}",
    )
    return 0


def pypi_release(version: str) -> JsonValue:
    """PyPI's JSON for this release, or None when it does not exist (404)."""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/kernal-api/{version}/json", timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise gha.StepError(f"PyPI returned HTTP {error.code}") from error


def published_digests(release: JsonValue) -> set[tuple[str, str]]:
    urls = release.get("urls", []) if isinstance(release, dict) else []
    pairs = set()
    for entry in urls if isinstance(urls, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("digests"), dict):
            pairs.add((str(entry.get("filename")), str(entry["digests"].get("sha256"))))
    return pairs


def publish_pypi(tag: str) -> int:
    version = tag.removeprefix("v")
    files = sorted(glob.glob(f"release-packages/kernal_api-{version}*"))
    release = pypi_release(version)
    if release is None:
        gha.run(["uv", "run", "--no-project", "--with", "twine", "twine", "upload", "--non-interactive", *files])
        return 0
    published = published_digests(release)
    for name in files:
        digest = hashlib.sha256(Path(name).read_bytes()).hexdigest()
        if (Path(name).name, digest) not in published:
            raise gha.StepError(f"PyPI's {Path(name).name} does not match the staged artifact")
    return 0


def existing_release(tag: str, repository: str) -> JsonValue:
    pages = gha.read(["gh", "api", f"repos/{repository}/releases", "--paginate", "--slurp"])
    if pages.returncode != 0:
        raise gha.StepError(f"listing releases exited {pages.returncode}")
    matches = [release for page in json.loads(pages.text) for release in page if release.get("tag_name") == tag]
    return matches[0] if matches else None


def release_assets() -> int:
    tag = os.environ["RELEASE_TAG"]
    repository = os.environ["GITHUB_REPOSITORY"]
    assets = sorted(glob.glob("release-assets/*"))
    release = existing_release(tag, repository)
    if release is None:
        gha.run(
            [
                "gh",
                "release",
                "create",
                tag,
                *assets,
                "--repo",
                repository,
                "--target",
                os.environ["RELEASE_SHA"],
                "--generate-notes",
            ]
        )
        return 0
    # Manual registry recovery must not overwrite published GitHub assets.
    if not isinstance(release, dict) or release.get("draft") is not False:
        raise gha.StepError(f"release {tag} exists but is not a published (non-draft) release")
    with tempfile.TemporaryDirectory() as download_dir:
        gha.run(["gh", "release", "download", tag, "--repo", repository, "--dir", download_dir])
        for asset in assets:
            published = Path(download_dir) / Path(asset).name
            if not published.is_file() or published.read_bytes() != Path(asset).read_bytes():
                raise gha.StepError(f"published asset {Path(asset).name} differs from {asset}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="release.yml steps")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify-version").add_argument("--tag", required=True)
    sub.add_parser("conpty")
    sub.add_parser("stage-registry")
    worker = sub.add_parser("stage-worker")
    worker.add_argument("--target", required=True)
    worker.add_argument("--extension", default="")
    sub.add_parser("publish-pypi").add_argument("--tag", required=True)
    sub.add_parser("release-assets")
    args = parser.parse_args(sys.argv[1:])
    if args.command == "verify-version":
        return verify_version(args.tag)
    if args.command == "stage-worker":
        return stage_worker(args.target, args.extension)
    if args.command == "publish-pypi":
        return publish_pypi(args.tag)
    return {"conpty": conpty, "stage-registry": stage_registry, "release-assets": release_assets}[args.command]()


if __name__ == "__main__":
    gha.main_guard(main)
