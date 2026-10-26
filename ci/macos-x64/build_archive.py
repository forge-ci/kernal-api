#!/usr/bin/env python3
"""Cross-build x86_64-apple-darwin test binaries on Linux.

Soldr carries its own blessed cross toolchain -- bundled LLVM plus a managed
macOS SDK -- so this produces real Mach-O executables with no Mac involved.
Measured locally: 25 binaries, 103 files, ~112 MB archive, ~85 s.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    out = Path(os.environ.get("OUT") or Path.cwd() / "kernal-x64.tar.zst")
    returncode = subprocess.run(
        [
            "soldr",
            "cargo",
            "nextest",
            "archive",
            "--target",
            "x86_64-apple-darwin",
            "--all-features",
            "--archive-file",
            str(out),
        ],
        check=False,
    ).returncode
    if returncode != 0:
        return returncode
    print(f"archive: {out.stat().st_size / (1024 * 1024):.0f}M -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
