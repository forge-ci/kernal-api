"""The single required context: fail when any job of this run attempt did not succeed.

It reads the run attempt's full job list instead of ``needs.*.result``: a
reusable-workflow job can end ``cancelled`` while its caller reports success,
and a job added later is covered without editing this gate or the ruleset.
Standard library only; the job-list call retries so a rate limit cannot turn a
green run red.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BLOCKING = frozenset({"failure", "cancelled", "timed_out", "startup_failure", "action_required"})
# This gate itself and the maintenance jobs that sweep caches after it.
EXEMPT_PREFIXES = ("CI summary", "Cache janitor", "Retire superseded")
ATTEMPTS = 6


def fetch(url: str, token: str) -> dict:
    """GET one page, retrying transient and rate-limit failures with backoff."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            if attempt == ATTEMPTS:
                raise
            delay = min(2**attempt, 60)
            print(f"job list call failed ({error}); retry {attempt}/{ATTEMPTS - 1} in {delay}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def list_jobs(repo: str, run_id: str, attempt: str, token: str) -> list[dict]:
    jobs: list[dict] = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}"
        body = fetch(url, token)
        jobs.extend(body.get("jobs", []))
        if len(jobs) >= body.get("total_count", 0) or not body.get("jobs"):
            return jobs
        page += 1


def blocking_jobs(jobs: list[dict]) -> list[dict]:
    return [job for job in jobs if not job["name"].startswith(EXEMPT_PREFIXES) and job.get("conclusion") in BLOCKING]


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    jobs = list_jobs(repo, run_id, attempt, os.environ["GITHUB_TOKEN"])
    for job in jobs:
        print(f"{job.get('conclusion') or job.get('status'):>16}  {job['name']}")
    blocked = blocking_jobs(jobs)
    if blocked:
        print(f"\nCI summary blocked by {len(blocked)} job(s):")
        for job in blocked:
            print(f"::error title=CI summary::{job['name']} concluded {job['conclusion']} ({job.get('html_url', '')})")
        return 1
    print(f"\nCI summary: {len(jobs)} job(s), none unsuccessful")
    return 0


if __name__ == "__main__":
    sys.exit(main())
