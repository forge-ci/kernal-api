#!/usr/bin/env python3
"""Fail unless the checked-out HEAD is exactly the selected source SHA.

Usage: ``python ci/verify_source_sha.py <expected-sha>``
"""

from __future__ import annotations

import sys

try:
    from ci import gha
except ImportError:  # run as a script: ci/ is sys.path[0]
    import gha


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1]:
        raise gha.StepError("usage: verify_source_sha.py <expected-sha>")
    head = gha.stdout_of(["git", "rev-parse", "HEAD"])
    if head != sys.argv[1]:
        raise gha.StepError(f"checked-out HEAD {head} is not the selected source {sys.argv[1]}")
    print(f"HEAD is {head}")
    return 0


if __name__ == "__main__":
    gha.main_guard(main)
