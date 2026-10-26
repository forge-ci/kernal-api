"""Exercise the screenshot guest builder against a fake Soldr.

Replaces the former tests/screenshot-build-arguments.sh,
tests/screenshot-target-repair.sh and tests/screenshot-target-repair.ps1: the
builder's argument construction (one storage path, never split; the fault
feature only on fault variants) and its target repair (verify files, not
bookkeeping; never mutate a healthy or unprobed toolchain; preserve an
existing manifest; never build after a failed repair).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILDER = REPO / "examples" / "wasm-tauri-screenshot" / "build_guest.py"
TARGET = "wasm32-wasip1-threads"

# A fake `soldr` that records every invocation and models one toolchain state.
FAKE_SOLDR = """\
import json, os, sys
from pathlib import Path
state = Path(os.environ["MOCK_STATE"])
data = json.loads(state.read_text())
args = sys.argv[1:]
line = " ".join(args)
with open(os.environ["MOCK_CALLS"], "a", encoding="utf-8") as calls:
    calls.write(json.dumps(args) + "\\n")
case = os.environ["MOCK_CASE"]
sysroot = Path(os.environ["MOCK_SYSROOT"])
libdir = sysroot / "lib"
def materialize(core_only=False):
    libdir.mkdir(parents=True, exist_ok=True)
    (libdir / "libcore-0.rlib").touch()
    if not core_only:
        (libdir / "libstd-0.rlib").touch()
if "rustc --print target-libdir" in line:
    if case == "probe-fails":
        sys.exit(19)
    print(libdir)
elif "rustc --print sysroot" in line:
    print(sysroot)
elif "rustup target remove" in line:
    if case == "remove-fails":
        sys.exit(17)
    data["removed"] = True
elif "rustup target add" in line:
    if case == "install" or (data.get("removed") and case != "still-missing"):
        materialize()
elif "cargo build" in line:
    target_dir = Path(args[args.index("--target-dir") + 1])
    out = target_dir / args[args.index("--target") + 1] / "release"
    out.mkdir(parents=True, exist_ok=True)
    (out / "kernal-api-wasm-tauri-guest.wasm").write_bytes(b"\\0asm")
elif "cargo run" in line:
    pass
else:
    print("unexpected invocation: " + line, file=sys.stderr)
    sys.exit(20)
state.write_text(json.dumps(data))
"""


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str
    calls: tuple[tuple[str, ...], ...]

    def ran(self, fragment: str) -> bool:
        return any(fragment in " ".join(call) for call in self.calls)

    def build_call(self) -> tuple[str, ...]:
        builds = [call for call in self.calls if call[:2] == ("cargo", "build")]
        assert len(builds) == 1, self.calls
        return builds[0]


class Harness:
    def __init__(self, case: unittest.TestCase):
        self.root = Path(tempfile.mkdtemp())
        case.addCleanup(shutil.rmtree, self.root)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        soldr = self.bin / "soldr"
        soldr.write_text(f"#!{sys.executable}\n{FAKE_SOLDR}", encoding="utf-8")
        soldr.chmod(0o755)
        self.sysroot = self.root / "sysroot"
        (self.sysroot / "lib" / "rustlib").mkdir(parents=True)
        self.manifest = self.sysroot / "lib" / "rustlib" / f"manifest-rust-std-{TARGET}"
        self.storage = self.root / "screenshot mock storage"

    def run(self, mock_case: str, *flags: str, target_dir: str | None = None) -> Result:
        libdir = self.sysroot / "lib"
        for name in ("libcore-0.rlib", "libstd-0.rlib"):
            (libdir / name).unlink(missing_ok=True)
        if mock_case == "healthy":
            (libdir / "libcore-0.rlib").touch()
            (libdir / "libstd-0.rlib").touch()
        if mock_case == "core-only":
            (libdir / "libcore-0.rlib").touch()
        state = self.root / "state.json"
        state.write_text("{}", encoding="utf-8")
        calls = self.root / "calls"
        calls.write_text("", encoding="utf-8")
        env = dict(
            os.environ,
            PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            MOCK_STATE=str(state),
            MOCK_CALLS=str(calls),
            MOCK_CASE=mock_case,
            MOCK_SYSROOT=str(self.sysroot),
            CARGO_TARGET_DIR=str(self.storage) if target_dir is None else target_dir,
        )
        with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
            returncode = subprocess.run(
                [sys.executable, str(BUILDER), *flags], env=env, stdout=out, stderr=err, check=False
            ).returncode
            out.seek(0)
            err.seek(0)
            stdout, stderr = out.read(), err.read()
        recorded = tuple(tuple(json.loads(line)) for line in calls.read_text(encoding="utf-8").splitlines() if line)
        return Result(returncode, stdout, stderr, recorded)


class ArgumentTests(unittest.TestCase):
    """One storage path, never split; the fault feature only on fault variants."""

    def test_variants(self):
        harness = Harness(self)
        for flags, feature, suffix in (
            ((), None, ""),
            (("--trap-after-capture",), "proof-trap-after-capture", "-trap"),
            (("--block-after-capture",), "proof-block-after-capture", "-block"),
        ):
            with self.subTest(flags=flags):
                result = harness.run("healthy", *flags)
                self.assertEqual(result.returncode, 0, result.stderr)
                build = result.build_call()
                self.assertEqual(build[build.index("--target") + 1], TARGET)
                self.assertEqual(
                    build[build.index("--target-dir") + 1],
                    str(harness.storage / f"kernal-api-wasm-tauri-guest{suffix}"),
                )
                if feature is None:
                    self.assertNotIn("--features", build)
                else:
                    self.assertEqual(build[build.index("--features") + 1], feature)
                admitted = Path(result.stdout.strip().splitlines()[-1])
                self.assertEqual(admitted.name, "kernal-api-wasm-tauri-guest.admitted.wasm")
                self.assertTrue(admitted.is_file())
                self.assertTrue(result.ran("--embed-threaded-metadata"))

    def test_both_faults_are_rejected(self):
        result = Harness(self).run("healthy", "--trap-after-capture", "--block-after-capture")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.calls, ())

    def test_relative_storage_is_rejected_before_any_tool_runs(self):
        result = Harness(self).run("healthy", target_dir="relative/storage")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CARGO_TARGET_DIR", result.stderr)
        self.assertEqual(result.calls, ())

    def test_other_targets_are_rejected_unless_only_preparing(self):
        harness = Harness(self)
        self.assertNotEqual(harness.run("healthy", "--target", "wasm32-unknown-unknown").returncode, 0)
        prepared = harness.run("healthy", "--prepare-target-only", "--target", "wasm32-unknown-unknown")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)


class TargetRepairTests(unittest.TestCase):
    """Verify files, not bookkeeping, and fail closed."""

    def test_cases(self):
        harness = Harness(self)
        for case in (
            "healthy",
            "install",
            "stale",
            "existing-manifest",
            "core-only",
            "remove-fails",
            "still-missing",
            "probe-fails",
        ):
            with self.subTest(case=case):
                harness.manifest.unlink(missing_ok=True)
                if case == "existing-manifest":
                    harness.manifest.write_text("preserve\n", encoding="utf-8")
                result = harness.run(case)
                failing = case in ("remove-fails", "still-missing", "probe-fails")
                if failing:
                    self.assertNotEqual(result.returncode, 0, f"unexpected success: {case}")
                    self.assertFalse(result.ran("cargo build"), "built after failed repair")
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                if case in ("healthy", "probe-fails"):
                    self.assertFalse(result.ran("rustup target"), "mutated healthy or unprobed toolchain")
                elif case == "install":
                    self.assertFalse(result.ran("rustup target remove"), "unnecessary removal")
                else:
                    self.assertTrue(result.ran("rustup target remove"), "repair not attempted")
                if case == "remove-fails":
                    adds = [call for call in result.calls if call[:3] == ("rustup", "target", "add")]
                    self.assertEqual(len(adds), 1, "continued after failed removal")
                if case == "existing-manifest":
                    self.assertEqual(harness.manifest.read_text(encoding="utf-8"), "preserve\n")
                elif case in ("stale", "core-only"):
                    self.assertTrue(harness.manifest.is_file())

    def test_prepare_target_only_never_builds(self):
        result = Harness(self).run("healthy", "--prepare-target-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.calls, (("rustc", "--print", "target-libdir", "--target", TARGET),))


if __name__ == "__main__":
    unittest.main()
