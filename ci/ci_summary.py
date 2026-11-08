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

Live-sibling exception (pull_request only): ``gh pr create --label`` fires
``opened`` and ``labeled`` seconds apart, so concurrency cancels one run while
its sibling is still queued or running and has no gate conclusion yet. For a
``pull_request`` run whose only non-success is cancellation, a same-SHA sibling
of this workflow that is ``queued`` or ``in_progress`` also counts: it posts its
own later ``CI summary``, which then wins, so a red sibling still blocks. It is
not extended to ``merge_group`` or ``push``: the merge queue merges on the
first green required context, so an early pass there could merge a commit
before the sibling's verdict exists.

A sibling run that completed with any conclusion other than ``success`` never
counts, even if its gate passed: that gate may itself have deferred to this run,
and two cancelled runs must not vouch for each other.
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
LIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
LIVE_SIBLING_EVENTS = frozenset({"pull_request"})


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


# Jobs that only judge other jobs of this run under `if: always()`. In a
# cancelled run they see cancelled `needs` and conclude `failure` as a
# consequence, not as independent evidence (observed on run 42372367018).
# They are tolerated in the superseded case only; the green sibling at the same
# SHA has already run its own copy of each to success.
DERIVED_AGGREGATES = frozenset({"Full coverage"})


def only_cancellations(blocked: list[dict]) -> bool:
    """True when the attempt's only non-success is its own cancellation."""
    cancelled = [job for job in blocked if job.get("conclusion") == "cancelled"]
    derived = [job for job in blocked if job["name"] in DERIVED_AGGREGATES and job.get("conclusion") == "failure"]
    return bool(cancelled) and len(cancelled) + len(derived) == len(blocked)


def green_summary(jobs: list[dict]) -> dict | None:
    for job in jobs:
        if job["name"].startswith(EXEMPT_PREFIXES) and job.get("conclusion") == "success":
            return job
    return None


def superseding_run(repo: str, run_id: str, token: str) -> str | None:
    """Return a description of the sibling run at this head SHA that this cancelled run defers to.

    Siblings are runs of the same workflow file at the same head SHA. Creation
    order is not used: duplicate runs can share a creation second (#600's two
    runs were both created 08:20:44Z). A sibling qualifies when its gate has
    already concluded success, or (pull_request only) when it is still live and
    will publish its own, later, ``CI summary``. A completed sibling that did
    not conclude ``success`` never qualifies.
    """
    own = fetch(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}", token)
    allow_live = own.get("event") in LIVE_SIBLING_EVENTS
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/{own['workflow_id']}/runs"
        f"?head_sha={own['head_sha']}&per_page=100"
    )
    for run in fetch(url, token).get("workflow_runs", []):
        if str(run["id"]) == str(run_id) or run.get("head_sha") != own["head_sha"]:
            continue
        status = run.get("status")
        if status == "completed" and run.get("conclusion") != "success":
            continue
        link = run.get("html_url", run["id"])
        summary = green_summary(list_jobs(repo, str(run["id"]), str(run.get("run_attempt", 1)), token))
        if summary is not None:
            return f"green {link} ({summary.get('html_url', '')})"
        if allow_live and status in LIVE_STATUSES:
            return f"live ({status}) run {run['id']} {link}, whose own CI summary will follow"
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
            print(f"\nCI summary: this run was cancelled, but it is superseded by {sibling}; passing")
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
