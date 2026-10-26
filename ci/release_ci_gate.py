"""Require a successful exact-SHA full CI dispatch before publication."""

import json
import os
import re
import sys
from urllib.request import Request, urlopen


def failures(run, jobs, candidate_sha, run_id):
    errors = []
    if not re.fullmatch(r"[0-9a-fA-F]{40}", candidate_sha):
        errors.append("candidate SHA is not a 40-character commit ID")
    if run.get("id") != int(run_id):
        errors.append("CI run ID mismatch")
    if run.get("head_sha", "").lower() != candidate_sha.lower():
        errors.append("CI run tested a different SHA")
    if run.get("event") != "workflow_dispatch" or not run.get("path", "").split("@", 1)[0].endswith(
        ".github/workflows/ci.yml"
    ):
        errors.append("CI run is not an explicit full candidate dispatch")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        errors.append("CI run did not complete successfully")
    coverage = [job for job in jobs if job.get("name") == "Full coverage"]
    if len(coverage) != 1 or coverage[0].get("conclusion") != "success":
        errors.append("Full coverage sentinel did not pass")
    return errors


def get_json(url):
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def main():
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["FULL_CI_RUN_ID"]
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("full CI run ID must be a positive integer")
    base = f"{api}/repos/{repo}/actions/runs/{run_id}"
    run = get_json(base)
    jobs = []
    page = 1
    while True:
        batch = get_json(f"{base}/jobs?per_page=100&page={page}")["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    errors = failures(run, jobs, os.environ["CANDIDATE_SHA"], run_id)
    for error in errors:
        print(f"::error::release CI gate: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"Exact-SHA full CI passed: {run_id} at {os.environ['CANDIDATE_SHA']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, ValueError) as error:
        print(f"::error::release CI gate: {error}", file=sys.stderr)
        raise SystemExit(1) from error
