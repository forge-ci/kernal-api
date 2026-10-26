"""Detect a main-branch version bump; fail closed on Git/API errors."""

import os
import re
import subprocess
import sys
from pathlib import Path

import tomllib


def release_tag(manifest: str) -> str:
    version = tomllib.loads(manifest)["package"]["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("release version must be a supported semantic version")
    if version == "0.0.0":
        raise ValueError("the namespace reservation must never be released")
    return f"v{version}"


def should_release(tag: str, previous_tag: str | None, manual: bool) -> bool:
    return manual or (previous_tag is not None and tag != previous_tag)


def main() -> None:
    tag = release_tag(Path("Cargo.toml").read_text(encoding="utf-8"))
    if sys.argv[1:] == ["--verify-source"]:
        if os.environ["RELEASE_TAG"] != tag:
            raise ValueError("release tag does not match Cargo.toml")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        if head != os.environ["RELEASE_SHA"]:
            raise ValueError("release source must be the candidate commit")
        tags = subprocess.check_output(["git", "tag", "--list", tag], text=True)
        if tags.strip():
            tagged = subprocess.check_output(["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"], text=True).strip()
            if tagged != head:
                raise ValueError("existing release tag points to a different commit")
        return
    if sys.argv[1:]:
        raise ValueError("unexpected command arguments")
    manual = os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch"
    previous_tag = None
    if not manual:
        before = os.environ["RELEASE_BEFORE"]
        if not re.fullmatch(r"[0-9a-f]{40}", before) or set(before) == {"0"}:
            raise ValueError("automatic release requires a valid previous commit")
        previous = subprocess.check_output(["git", "show", f"{before}:Cargo.toml"], text=True)
        # The first usable release may follow the namespace reservation.
        previous_tag = "v" + tomllib.loads(previous)["package"]["version"]
    proceed = should_release(tag, previous_tag, manual)
    if proceed and not manual:
        existing = subprocess.check_output(
            [
                "gh",
                "api",
                f"repos/{os.environ['GITHUB_REPOSITORY']}/releases",
                "--paginate",
                "--jq",
                ".[].tag_name",
            ],
            text=True,
        ).splitlines()
        proceed = tag not in existing
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"tag={tag}\nshould_release={str(proceed).lower()}\n")
    print(f"{tag}: {'verify release' if proceed else 'no new release'}")


if __name__ == "__main__":
    main()
