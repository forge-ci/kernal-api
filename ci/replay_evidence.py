"""Require executed minimal checks using the shared Bosn evidence verifier.

This is runtime evidence, not a waiver of the shared static coverage check.
Full-mode checks retain their existing remote selection and are not attested.
"""

from pathlib import Path

from ci_lint.cargo_messages import JsonValue
from ci_lint.workflow_replay import ReplayExpectation, ReplayJob, prove_replay
from ci_lint.workflow_replay_identity import identity_part

VERIFY = ReplayJob(
    "CI/verify",
    identity=(identity_part("verify"),),
    steps=(
        "Checkout candidate source",
        "Checkout pinned ci-lint",
        "Set up uv",
        "Verify local gate attestation",
    ),
)
LINUX = ReplayJob(
    "CI/linux",
    identity=(identity_part("linux"),),
    steps=(
        "Checkout candidate source",
        "Set up uv",
        "Select CI mode and verify the checked-out commit",
        "Verify selected source SHA",
        "Checkout pinned ci-lint",
        "Plan this run with the pinned ci-lint",
        "Precheck the repository with the pinned ci-lint",
        "Set up soldr",
        "Restore native pkg-config search path",
        "Check the CI guards and the guest build scripts",
        "Scan the repository against the platform boundary",
        "Test the default facade",
        "Test the Linux desktop status facade",
    ),
)
def verify_checks(raw: JsonValue, workspace: Path, sha: str, git_tree: str, job: str) -> None:
    """A green aggregate cannot replace a skipped or missing selected check."""
    # lint never runs under bosn; soldr lint only. The `dylints` job (and the
    # formatting/Clippy steps moved into it) is attested by the host `lint` lane.
    if job != "linux":
        raise ValueError("unknown minimal replay job")
    expected = ReplayExpectation(
        # The record names the checkout's origin; either spelling is this repository (#436).
        repository="forge-ci/kernal-api",
        repository_aliases=("zackees/kernal-api",),
        workspace=workspace,
        sha=sha,
        git_tree=git_tree,
        workflow=".github/workflows/ci.yml",
        selected_job=job,
        mode="minimal",
        required_jobs=(VERIFY, LINUX),
    )
    prove_replay(raw, expected)
