"""Verify that a full CI run completed every required matrix leg on one SHA."""

import json
import os
import re
import subprocess
import sys
from urllib.request import Request, urlopen

TARGETS = (
    "aarch64-unknown-linux-gnu",
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
)
REQUIRED_JOBS = (
    "linux",
    *(f"Build ({target})" for target in TARGETS),
    *(f"Test ({target})" for target in TARGETS),
    "Dylint workspace",
)
REQUIRED_NEEDS = ("linux", "build", "test", "dylints")
REQUIRED_TEST_STEPS = (
    "Run this host's prebuilt tests",
    "Prove this host ran its own code",
    "Run actual Core and Component compiler-policy guest proofs",
    "Run offline native Wasm screenshot, admission, and worker containment proofs",
)


def failures(expected_sha, checked_out, needs, jobs):  # noqa: C901 -- Existing full-coverage aggregation; ratchet complexity 13.
    errors = []
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_sha):
        errors.append("source SHA is missing or invalid")
    if checked_out.lower() != expected_sha.lower():
        errors.append(f"source SHA mismatch: checked out {checked_out}, expected {expected_sha}")
    for name in REQUIRED_NEEDS:
        dependency = needs.get(name)
        result = dependency.get("result", "missing") if isinstance(dependency, dict) else "missing"
        if result != "success":
            errors.append(f"{name}: {result}")
    by_name = {}
    for job in jobs:
        by_name.setdefault(job.get("name"), []).append(job)
    for name in REQUIRED_JOBS:
        results = by_name.get(name, [])
        if not results:
            errors.append(f"{name}: missing")
        elif len(results) != 1:
            errors.append(f"{name}: expected one job, found {len(results)}")
        elif results[0].get("conclusion") != "success":
            errors.append(f"{name}: {results[0].get('conclusion') or 'pending'}")
        elif name.startswith("Test ("):
            steps = {step.get("name"): step.get("conclusion") for step in results[0].get("steps", [])}
            for required in REQUIRED_TEST_STEPS:
                if steps.get(required) != "success":
                    errors.append(f"{name}: {required}: {steps.get(required) or 'missing'}")
    return errors


def run_jobs():
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    token = os.environ["GITHUB_TOKEN"]
    jobs = []
    page = 1
    while True:
        url = f"{api}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&page={page}"
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        with urlopen(request, timeout=30) as response:
            batch = json.load(response)["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            return jobs
        page += 1


def checked_out_sha() -> str:
    """HEAD of the checkout this job verifies (stdout iterated, not captured)."""
    with subprocess.Popen(["git", "rev-parse", "HEAD"], stdout=subprocess.PIPE, text=True) as git:
        assert git.stdout is not None
        sha = "".join(git.stdout).strip()
    if git.returncode != 0 or not sha:
        raise SystemExit("cannot read the checked-out HEAD")
    return sha


def main():
    errors = failures(
        os.environ["EXPECTED_SHA"],
        os.environ.get("CHECKED_OUT_SHA") or checked_out_sha(),
        json.loads(os.environ["CI_NEEDS_JSON"]),
        run_jobs(),
    )
    for error in errors:
        print(f"::error::full CI coverage incomplete: {error}")
    if errors:
        return 1
    print(f"Full CI coverage complete at {os.environ['EXPECTED_SHA']}: {len(REQUIRED_JOBS)} jobs")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, ValueError) as error:
        print(f"::error::full CI coverage check failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
