"""The single required context: fail when any job of this run attempt did not succeed.

It reads the run attempt's full job list instead of ``needs.*.result``: a
reusable-workflow job can end ``cancelled`` while its caller reports success,
and a job added later is covered without editing this gate or the ruleset.
Standard library only; the job-list call retries so a rate limit cannot turn a
green run red.

Superseded-run exception (kernal-api#600 incident): GitHub resolves a required
context by the *latest* check run of that name at the head SHA, so a duplicate
run that is cancelled after a sibling run already went green would otherwise
post a later ``failure`` and block the PR forever. When -- and only when --
every blocking job of this attempt concluded ``cancelled`` AND another run of
this same workflow at the same head SHA already has a ``CI summary`` job that
concluded ``success`` (read from the API, with retries), this gate passes and
names the run it defers to. Any failure, timeout, startup failure or approval
wait still fails, as does a cancellation with no green sibling.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BLOCKING = frozenset({"failure", "cancelled", "timed_out", "startup_failure", "action_required"})
# Only the gate itself is exempt: it is still running while it reads the job
# list, so it cannot police its own conclusion. Maintenance jobs are not exempt:
# the gate `needs` cache-retention, so a skipped one passes and a failed one
# blocks. The cache janitor must run after this gate (GEN-022), so it is still
# queued here and has no conclusion to judge; it is not hidden by name either.
EXEMPT_PREFIXES = ("CI summary",)
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
    """Every job of one terminal attempt; the page count is fixed by the first page."""
    base = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
    first = fetch(f"{base}&page=1", token)
    jobs: list[dict] = list(first.get("jobs", []))
    pages = -(-int(first.get("total_count", 0)) // 100)
    for page in range(2, pages + 1):
        jobs.extend(fetch(f"{base}&page={page}", token).get("jobs", []))
    return jobs


def blocking_jobs(jobs: list[dict]) -> list[dict]:
    return [job for job in jobs if not job["name"].startswith(EXEMPT_PREFIXES) and job.get("conclusion") in BLOCKING]


def only_cancellations(blocked: list[dict]) -> bool:
    return bool(blocked) and all(job.get("conclusion") == "cancelled" for job in blocked)


def green_summary(jobs: list[dict]) -> dict | None:
    for job in jobs:
        if job["name"].startswith(EXEMPT_PREFIXES) and job.get("conclusion") == "success":
            return job
    return None


def superseding_run(repo: str, run_id: str, token: str) -> str | None:
    """Return the URL of a sibling run at this head SHA whose CI summary already succeeded.

    Siblings are runs of the same workflow file at the same head SHA. Creation
    order is not used: duplicate runs can share a creation second (#600's two
    runs were both created 08:20:44Z). What matters is that the sibling's gate
    has already *concluded* success, so this run's later context is the one
    that would otherwise win.
    """
    own = fetch(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}", token)
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/{own['workflow_id']}/runs"
        f"?head_sha={own['head_sha']}&per_page=100"
    )
    for run in fetch(url, token).get("workflow_runs", []):
        if str(run["id"]) == str(run_id) or run.get("head_sha") != own["head_sha"]:
            continue
        summary = green_summary(list_jobs(repo, str(run["id"]), str(run.get("run_attempt", 1)), token))
        if summary is not None:
            return f"{run.get('html_url', run['id'])} ({summary.get('html_url', '')})"
    return None


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    token = os.environ["GITHUB_TOKEN"]
    jobs = list_jobs(repo, run_id, attempt, token)
    for job in jobs:
        print(f"{job.get('conclusion') or job.get('status'):>16}  {job['name']}")
    blocked = blocking_jobs(jobs)
    if only_cancellations(blocked):
        sibling = superseding_run(repo, run_id, token)
        if sibling is not None:
            print(f"\nCI summary: this run was cancelled, but it is superseded by green {sibling}; passing")
            return 0
    if blocked:
        print(f"\nCI summary blocked by {len(blocked)} job(s):")
        for job in blocked:
            print(f"::error title=CI summary::{job['name']} concluded {job['conclusion']} ({job.get('html_url', '')})")
        return 1
    print(f"\nCI summary: {len(jobs)} job(s), none unsuccessful")
    return 0


if __name__ == "__main__":
    sys.exit(main())
