#!/usr/bin/env python3
"""Hand ci/macos-x64/recovery-guest.sh to the next step as the `script` output.

Recovery boots exactly one script and offers no per-command execution, so the
guest half travels as a single string. A step output keeps recovery-guest.sh a
reviewable file rather than an inline heredoc.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DELIMITER = "KERNAL_GUEST_SCRIPT_EOF"
GUEST_SCRIPT = Path(__file__).resolve().parent / "recovery-guest.sh"


def main() -> int:
    script = GUEST_SCRIPT.read_text(encoding="utf-8")
    if DELIMITER in script:
        print(f"{GUEST_SCRIPT} contains the output delimiter {DELIMITER}", file=sys.stderr)
        return 1
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"script<<{DELIMITER}\n{script.rstrip(chr(10))}\n{DELIMITER}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
