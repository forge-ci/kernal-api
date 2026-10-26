#!/usr/bin/env python3
"""Regenerate the Wasm ABI into scratch storage and diff it against the tree.

Explicit output redirection (KERNAL_API_ABI_OUTPUT) makes drift verification
read-only for the checked-in artifacts.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    generated_dir = REPO / "src" / "wasm" / "generated" / "v1"
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch) / "generated"
        environment = dict(os.environ, KERNAL_API_ABI_OUTPUT=str(output))
        manifest = REPO / "tools" / "wasm-abi-generator" / "Cargo.toml"
        generated = subprocess.run(
            ["soldr", "cargo", "run", "--locked", "--manifest-path", str(manifest)], env=environment, check=False
        )
        if generated.returncode != 0:
            return generated.returncode
        return subprocess.run(["diff", "-ru", "-x", "target", str(generated_dir), str(output)], check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
