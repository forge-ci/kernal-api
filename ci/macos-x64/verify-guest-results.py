"""Decide the macOS Recovery guest verdict from the evidence it sent back.

The guest runs one script and never exits non-zero, so the pass/fail decision
lives here: a guest boot costs minutes and its exit status cannot distinguish a
failed test from a failed transfer. This reads the collected files and returns
a verdict the workflow gates on.

It also encodes the reason the lane exists. `x86_64-apple-darwin` is the only
architecture that compiles the `frame_pointer_tests` module in
`src/snapshot/unwind.rs`, so naming those tests is what proves the guest ran
Intel code rather than an empty or wholly filtered partition. A green job that
executes nothing would be worse than the red one this lane replaces.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Compiled only under `#[cfg(all(test, target_arch = "x86_64"))]` in
# src/snapshot/unwind.rs. Their presence is the coverage proof: no other
# supported architecture produces these test names at all.
REQUIRED_X86_64_ONLY_TESTS = (
    "frame_pointer_fallback_walks_a_bounded_monotonic_chain",
    "frame_pointer_fallback_rejects_unaligned_or_backward_chains",
    "frame_pointer_fallback_stops_at_an_unattributed_return_address",
    "recovered_chain_replaces_the_suspect_framehop_tail",
    "recovered_chain_preserves_duplicates_and_valid_framehop_prefix",
    "recovered_chain_without_overlap_keeps_only_the_valid_prefix",
)

# src/crash/mod.rs tests are not architecture-gated, so one occurrence is
# enough to show the crash facade executed in the guest.
REQUIRED_SUBSTRINGS = ("crash::",)

# A floor, not a target: the named-test check above is the real assertion. It
# catches a partition that collapsed -- a filter that excluded far more than
# the named guest limitations, or an archive that lost its test binaries.
# Measured for x86_64-apple-darwin: 973 selected by the policy filter, 18 of
# them excluded by name in recovery-guest.sh, so ~955 is expected. The floor
# sits below that to absorb test-count drift without absorbing a collapse.
MINIMUM_TESTS_RUN = 900

STAGE_FAILURE = "stage-failure.txt"
NEXTEST_RC = "nextest.rc"
NEXTEST_LOG = "nextest.log"

SUMMARY_RE = re.compile(r"(\d+) tests run: (\d+) passed(?:, (\d+) skipped)?")


def last_summary(log: str) -> re.Match[str] | None:
    """Return the final `N tests run: P passed` summary nextest emits."""
    matches = list(SUMMARY_RE.finditer(log))
    return matches[-1] if matches else None


def missing_tests(log: str) -> list[str]:
    missing = [name for name in REQUIRED_X86_64_ONLY_TESTS if name not in log]
    missing.extend(item for item in REQUIRED_SUBSTRINGS if item not in log)
    return missing


def evaluate(collected: Path) -> tuple[bool, str]:
    """Return (ok, report). Never raises; every failure has a readable cause."""
    if not collected.is_dir():
        return False, f"no collected directory at {collected} -- the guest produced no evidence"

    stage_failure = collected / STAGE_FAILURE
    if stage_failure.is_file():
        detail = stage_failure.read_text(encoding="utf-8", errors="replace").strip()
        return False, f"guest staging failed: {detail}"

    rc_file = collected / NEXTEST_RC
    if not rc_file.is_file():
        return False, f"no {NEXTEST_RC} in {collected} -- nextest never ran in the guest"
    rc_text = rc_file.read_text(encoding="utf-8", errors="replace").strip()
    if rc_text != "0":
        return False, f"nextest exited {rc_text} in the guest; see the collected {NEXTEST_LOG}"

    log_file = collected / NEXTEST_LOG
    if not log_file.is_file():
        return False, f"no {NEXTEST_LOG} in {collected}"
    log = log_file.read_text(encoding="utf-8", errors="replace")

    summary = last_summary(log)
    if summary is None:
        return False, f"no nextest summary line in {NEXTEST_LOG}; see the collected log"

    total, passed = int(summary.group(1)), int(summary.group(2))
    skipped = int(summary.group(3) or 0)

    if total < MINIMUM_TESTS_RUN:
        return False, (
            f"only {total} tests ran (floor {MINIMUM_TESTS_RUN}); the partition looks collapsed rather than filtered"
        )
    if passed < total - skipped:
        return False, f"{total - skipped - passed} tests did not pass in the guest"

    missing = missing_tests(log)
    if missing:
        return False, (
            f"the guest ran, but the run does not prove Intel coverage; missing from the log: {', '.join(missing)}"
        )

    version_file = collected / "nextest-version.txt"
    version = (
        version_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0]
        if version_file.is_file()
        else "unknown"
    )
    return True, (
        f"guest executed {total} tests on x86_64-apple-darwin "
        f"({passed} passed, {skipped} skipped) with {version}; "
        f"all {len(REQUIRED_X86_64_ONLY_TESTS)} x86_64-only unwind tests ran"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collected", required=True, type=Path)
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="file to append the verdict to (GITHUB_STEP_SUMMARY)",
    )
    args = parser.parse_args(argv)

    ok, report = evaluate(args.collected)
    print(report)
    if args.summary is not None:
        heading = "macOS x86_64 Recovery guest: PASS" if ok else "macOS x86_64 Recovery guest: FAIL"
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(f"## {heading}\n\n{report}\n")

    if not ok:
        log = args.collected / NEXTEST_LOG
        if log.is_file():
            print("\n--- nextest log tail ---", file=sys.stderr)
            print(log.read_text(encoding="utf-8", errors="replace")[-4000:], file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
