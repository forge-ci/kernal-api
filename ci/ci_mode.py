"""Select a CI tier and the exact source commit for each event."""

import json
import os
import re
import sys
from pathlib import Path


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise ValueError("candidate SHA must be 40 hexadecimal characters")
    return value.lower()


def select(event_name, event, github_sha):
    if event_name == "workflow_dispatch":
        return "full", _sha(event.get("inputs", {}).get("candidate_sha"))
    if event_name in ("push", "merge_group"):
        # A queue run validates the exact merge commit at the push tier.
        return "minimal", _sha(github_sha)
    if event_name == "pull_request":
        pr = event["pull_request"]
        labels = {label["name"] for label in pr.get("labels", [])}
        mode = "full" if "ci-full" in labels else "test" if "ci-test" in labels else "minimal"
        return mode, _sha(pr["head"]["sha"])
    raise ValueError(f"unsupported CI event: {event_name}")


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    mode, sha = select(os.environ["GITHUB_EVENT_NAME"], event, os.environ["GITHUB_SHA"])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"mode={mode}\nsha={sha}\n")
    print(f"CI mode: {mode}; source SHA: {sha}")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError) as error:
        print(f"::error::{error}", file=sys.stderr)
        raise SystemExit(1) from error
