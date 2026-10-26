"""Prove nightly checksum freshness reuses contents and rejects changed inputs."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Result:
    code: int
    output: str


def check(root: Path, *, value: str = "A", feature: bool = False) -> Result:
    """Capture to a file so an inherited pipe cannot hold the caller open."""
    # setup-soldr enables colour globally; fixture diagnostics are machine-read.
    env = dict(os.environ, PROBE_VALUE=value, CARGO_TERM_COLOR="never")
    command = [
        "soldr",
        "rustup",
        "run",
        os.environ["DYLINT_TOOLCHAIN"],
        "cargo",
        "check",
        "--offline",
        "--verbose",
    ]
    if feature:
        command += ["--features", "broken"]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
        completed = subprocess.run(
            command,
            cwd=root,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output.seek(0)
        return Result(completed.returncode, output.read())


def passed(result: Result) -> None:
    if result.code:
        raise RuntimeError(result.output[-4000:])


def rejected(result: Result, reason: str) -> None:
    if not result.code or reason not in result.output:
        raise RuntimeError(f"changed input was not rejected: {reason}\n{result.output[-4000:]}")


def write_older(path: Path, value: str) -> None:
    """Same-size contents with an old mtime must still invalidate a checksum."""
    if len(value.encode()) != path.stat().st_size:
        raise ValueError("fixture replacement must preserve file size")
    path.write_text(value, encoding="utf-8")
    os.utime(path, (1, 1))


def probe(root: Path) -> None:
    config = root / ".cargo/config.toml"
    config.parent.mkdir()
    config.write_text((ROOT / ".cargo/config.toml").read_text(), encoding="utf-8")
    (root / "Cargo.toml").write_text(
        '[package]\nname="dylint-freshness-probe"\nversion="0.0.0"\nedition="2024"\n'
        "[workspace]\n[features]\nbroken=[]\n"
        '[lints.rust]\nunexpected_cfgs={level="deny",check-cfg=["cfg(broken_config)"]}\n',
        encoding="utf-8",
    )
    source = root / "src/lib.rs"
    source.parent.mkdir()
    source.write_text(
        "pub const VALUE: bool = true;\n"
        "const _: () = assert!(include_str!(\"value.txt\").as_bytes()[0] == b'A');\n"
        "const _: () = assert!(include_bytes!(\"value.bin\")[0] == b'A');\n"
        "const _: () = assert!(env!(\"PROBE_VALUE\").as_bytes()[0] == b'A');\n"
        '#[cfg(feature="broken")] compile_error!("changed feature");\n'
        '#[cfg(broken_config)] compile_error!("changed config");\n',
        encoding="utf-8",
    )
    text = root / "src/value.txt"
    binary = root / "src/value.bin"
    text.write_text("A", encoding="utf-8")
    binary.write_text("A", encoding="utf-8")
    passed(check(root))
    for path in (source, text, binary):
        os.utime(path, (time.time() + 20, time.time() + 20))
    unchanged = check(root)
    passed(unchanged)
    if "Fresh dylint-freshness-probe" not in unchanged.output:
        raise RuntimeError(f"identical sources recompiled after mtime change\n{unchanged.output[-4000:]}")
    original = source.read_text(encoding="utf-8")
    write_older(source, original.replace("true", "nope", 1))
    rejected(check(root), "E0425")
    write_older(source, original)
    passed(check(root))
    for path in (text, binary):
        write_older(path, "B")
        rejected(check(root), "E0080")
        write_older(path, "A")
        passed(check(root))
    rejected(check(root, value="B"), "E0080")
    passed(check(root))
    rejected(check(root, feature=True), "changed feature")
    passed(check(root))
    original_config = config.read_text(encoding="utf-8")
    config.write_text(original_config + '\n[build]\nrustflags=["--cfg", "broken_config"]\n')
    rejected(check(root), "changed config")
    config.write_text(original_config, encoding="utf-8")
    passed(check(root))
    # Build-script watched inputs still use mtimes: change them normally,
    # rather than promising checksum protection Cargo does not provide there.
    (root / "build.rs").write_text(
        'fn main() { println!("cargo:rerun-if-changed=build-input.txt"); '
        'println!("cargo:rustc-env=BUILD_VALUE={}", '
        'std::fs::read_to_string("build-input.txt").unwrap()); }\n',
        encoding="utf-8",
    )
    watched = root / "build-input.txt"
    watched.write_text("A", encoding="utf-8")
    source.write_text(original + "const _: () = assert!(env!(\"BUILD_VALUE\").as_bytes()[0] == b'A');\n")
    passed(check(root))
    watched.write_text("B", encoding="utf-8")
    rejected(check(root), "E0080")
    print("PASS checksum freshness: unchanged source; changed source/includes/env/feature/config/build input")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="dylint-freshness-") as directory:
        probe(Path(directory))


if __name__ == "__main__":
    main()
