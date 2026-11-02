"""TEMPORARY diagnostic (to be reverted): reproduce the aarch64-linux stalls and dump state."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_proof as proof

PATTERN = ("kernal", "WebKit", "rustc", "cc1", "collect2", "ld", "bwrap", "xdg-", "dbus", "Xvfb", "wasm_tauri", "cc ")


def sh(command: str, timeout: int = 90) -> None:
    print(f"$ {command}", flush=True)
    try:
        result = subprocess.run(command, shell=True, text=True, capture_output=True, timeout=timeout, errors="replace")
        print(result.stdout + result.stderr, flush=True)
    except subprocess.TimeoutExpired:
        print(f"(timed out after {timeout}s)", flush=True)


def dump(label: str) -> None:
    print(f"::group::STALL DUMP {label}", flush=True)
    sh("ps -eo pid,ppid,pgid,sid,stat,etimes,wchan:32,args --forest | grep -v ' \\[' | tail -120")
    listing = subprocess.run(["ps", "-eo", "pid=,args="], text=True, capture_output=True).stdout
    for line in listing.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid == str(os.getpid()) or "ps -eo" in args or "gdb" in args or "diag_aarch64" in args:
            continue
        if any(token in args for token in PATTERN) and "uv run" not in args:
            print(f"--- pid {pid}: {args[:200]}", flush=True)
            sh(
                f"sudo cat /proc/{pid}/wchan; echo; sudo cat /proc/{pid}/stack; sudo ls -l /proc/{pid}/fd | head -40",
                20,
            )
            sh(
                f"sudo gdb -p {pid} -batch -nx -ex 'set pagination off' -ex 'thread apply all bt 30' 2>&1 | grep -v '^\\[New LWP' | head -300",
                120,
            )
    print("::endgroup::", flush=True)


def watched(arguments: list, env: dict, label: str, stall_after: float, hard: float) -> tuple[int, float, str]:
    start = time.monotonic()
    process = subprocess.Popen(
        [str(a) for a in arguments],
        env=env,
        text=True,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.extend(process.stdout), daemon=True)
    reader.start()
    dumped = False
    while process.poll() is None:
        elapsed = time.monotonic() - start
        if not dumped and elapsed > stall_after:
            dumped = True
            dump(f"{label} at {elapsed:.1f}s")
        if elapsed > hard:
            process.kill()
            break
        time.sleep(0.2)
    process.wait()
    reader.join(5)
    return process.returncode, time.monotonic() - start, "".join(lines)


def rustc_loop(iterations: int) -> None:
    print("::group::rustc identity", flush=True)
    sh(
        "command -v rustc cc ld; readlink -f $(command -v rustc); ls -la ~/.cargo/bin 2>&1 | head; rustup show 2>&1 | head -20; rustc -vV; cc --version | head -1; ld --version | head -1; env | grep -iE 'rust|cargo' "
    )
    print("::endgroup::", flush=True)
    source = Path(tempfile.mkdtemp()) / "fixture.rs"
    source.write_text(
        '#[no_mangle] #[inline(never)] pub extern "C" fn t(v: u64) -> u64 { v.wrapping_mul(3) }\nfn main() { println!("{}", t(std::env::args().count() as u64)); }\n'
    )
    worst = 0.0
    for index in range(iterations):
        threads = []
        results: list[tuple[str, int, float]] = []

        def one(tag: str, arg: str) -> None:
            out = source.with_name(f"{tag}-{index}")
            code, elapsed, text = watched(
                ["rustc", "-g", "-Cdebuginfo=2", f"-Clink-arg=-Wl,{arg}", "-o", out, source],
                dict(os.environ),
                f"rustc {tag} #{index}",
                20,
                150,
            )
            results.append((tag, code, elapsed))
            if code:
                print(text[-2000:], flush=True)

        for tag, arg in (
            ("id", "--build-id"),
            ("none", "--build-id=none"),
            ("id2", "--build-id"),
            ("none2", "--build-id=none"),
            ("id3", "--build-id"),
        ):
            thread = threading.Thread(target=one, args=(tag, arg))
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        worst = max([worst, *(e for _, _, e in results)])
        print(f"rustc iteration {index}: " + ", ".join(f"{t}={c}/{e:.2f}s" for t, c, e in results), flush=True)
    print(f"rustc worst {worst:.2f}s", flush=True)


def screenshot_loop(native_target: str, bundle: Path, guests: Path, work: Path, iterations: int) -> None:
    root, manifest = proof.unpack(native_target, bundle, guests, work)
    base = dict(
        os.environ,
        KERNAL_API_SCREENSHOT_ARTIFACT_WASM=str(guests / proof.GUESTS["screenshot"]),
        KERNAL_API_SCREENSHOT_TRAP_ARTIFACT_WASM=str(guests / proof.GUESTS["screenshot-trap"]),
        KERNAL_API_SCREENSHOT_BLOCK_ARTIFACT_WASM=str(guests / proof.GUESTS["screenshot-block"]),
        KERNAL_API_THREADED_ARTIFACT_WASM=str(guests / proof.GUESTS["threaded"]),
    )
    harness = proof.role_test(root, manifest, "screenshot")
    env = proof.role_env(root, manifest, "screenshot", base)
    tests = [
        "actual_screenshot_guest_block_inside_containment_forces_reap",
        "actual_screenshot_guest_cancellation_inside_containment_drains_load",
    ]
    for index in range(iterations):
        arguments = proof.with_display(
            [harness, "--ignored", "--nocapture", "--test-threads=1", "--exact", *tests], env
        )
        code, elapsed, text = watched(arguments, env, f"screenshot #{index}", 9, 240)
        ok = " 2 passed" in text
        print(f"screenshot iteration {index}: exit={code} passed={ok} {elapsed:.1f}s", flush=True)
        if not ok:
            print(text[-6000:], flush=True)


def main() -> None:
    native_target, bundle, guests, work = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
    if shutil.which("gdb") is None:
        sh("sudo apt-get install -y --no-install-recommends gdb", 300)
    rustc_loop(int(os.environ.get("DIAG_RUSTC", "40")))
    screenshot_loop(native_target, bundle, guests, work, int(os.environ.get("DIAG_SHOTS", "25")))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # pylint: disable=broad-exception-caught  # diagnostic only
        print(f"diagnostic error: {error!r}", flush=True)
