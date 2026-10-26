#!/usr/bin/env python3
"""The multi-command checks of ci.yml's `linux` job (GEN-005).

Each subcommand is one workflow step; the step keeps its name, `if:` and
`env:` in the workflow, and its commands live here. Every Rust tool runs
through Soldr's front door.

Subcommands: ci-guards, boundary-scan, dylint-fmt, desktop-status [--full],
release-tooling, python-package, abi-generation, feature-isolation,
external-consumers, webview-proofs, screenshot-cli.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

try:
    from ci import gha
except ImportError:  # run as a script: ci/ is sys.path[0]
    import gha

REPO = gha.REPO
UV = ["uv", "run", "--no-project"]
BOUNDARY_CRATE = Path("dylints/kernal_api_platform_boundary")
# The CI guards and the guest-builder tests, run once on Linux.
CI_GUARD_MODULES = (
    "ci.test_native_proof",
    "ci.test_native_proof_jobs",
    "ci.test_macos_x64_guest",
    "ci.test_ci_modes",
    "ci.test_release_ci_gate",
    "ci.test_nextest_config",
    "ci.test_deny_warnings",
    "ci.test_target_features",
    "ci.test_local_gate",
    "ci.test_replay_evidence",
    "ci.test_cache_policy",
    "ci.test_prune_obsolete_cook_caches",
    "ci.test_build_guest",
    "ci.test_workflow_steps",
)
# Every feature checked in isolation.
ISOLATED_FEATURES = (
    "allocator",
    "archive",
    "archive-auth-test-support",
    "conpty-sidecar",
    "crash",
    # The four daemon lanes are the frozen running-process substrates. Without
    # them these features are compiled only inside `--all-features` in
    # `test-archive`, where every other feature is present to supply whatever
    # a missing gate would need -- exactly the coupling this matrix catches.
    "daemon-frame-v1",
    "daemon-identity",
    "daemon-registration",
    "daemon-registration-v2",
    # Broker client adapter over running-process/client (heavy graph).
    "broker-client",
    # Build-script helper (Windows resources) over embed-resource.
    "build-resources",
    "fs",
    "fs-watch",
    "hash-sha256",
    "secure-random",
    "text-similarity",
    "command-arguments",
    "config-toml",
    "json",
    "source-cpp",
    "terminal-style",
    "terminal-input",
    "event-stream",
    "http-server",
    "http-client",
    # Canonical scheduler/broker spawn API is public only through this
    # feature. Keep it isolated from the default process HAL.
    "independent-spawn",
    "ipc",
    "ipc-async",
    "profile",
    "pty",
    "session-relay",
    "snapshot",
    "symbolize",
    "symbolize-split",
    "symbolize-worker",
    "tokio-console",
    "tauri-webview",
    "system-tray",
    "window-icon",
)
SMOKE_MODES_BEFORE_POPUP = ("close", "widget", "bootstrap")
SMOKE_MODES_AFTER_POPUP = (
    "redirect",
    "timeout",
    "cancel",
    "window-close",
    "capture",
    "capture-cancel",
    "open-cancel",
)
UNDER_DISPLAY = ["dbus-run-session", "--", "xvfb-run", "-a"]
SMOKE = ["soldr", "cargo", "run", "--locked", "--all-features", "--bin", "kernal-tauri-smoke", "--"]
POPUP_WINDOW = "kernal-api external-content proof"
SCREENSHOT_CLI_TEST = "default_screenshot_cli_uses_containment_and_commits_output"
ONE_PASSED = "test result: ok. 1 passed; 0 failed;"


def ci_guards() -> int:
    gha.run([*UV, "--with", "pyyaml==6.0.2", "-m", "unittest", *CI_GUARD_MODULES])
    return 0


def boundary_scan() -> int:
    """The platform boundary's repository scan, on the lint's own nightly pin.

    Deliberately NOT run from the lint crate: its .cargo/config.toml sets
    `linker = dylint-link`, which this scan job does not install, so every
    build script fails to link. The scan is plain lib unit tests and needs
    no Dylint linker.
    """
    text = (BOUNDARY_CRATE / "rust-toolchain.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^channel = "(.*)"$', text)
    if not match:
        raise gha.StepError("rust-toolchain.toml names no channel")
    toolchain = match.group(1)
    gha.run(
        [
            "soldr",
            "rustup",
            "toolchain",
            "install",
            toolchain,
            "--profile",
            "minimal",
            "--component",
            "rustc-dev,llvm-tools-preview,rust-src",
        ]
    )
    manifest = str(BOUNDARY_CRATE / "Cargo.toml")
    # Lib unit tests only: the crate's dylint-link linker is not installed here.
    test = ["soldr", "rustup", "run", toolchain, "cargo", "test", "--locked", "--lib"]
    test += ["--manifest-path", manifest]  # ci-lint: allow RUST-016 the scan needs no Dylint linker
    gha.run([*test, "--", "--skip", "ui"])
    return 0


def dylint_fmt() -> int:
    for manifest in sorted(glob.glob("dylints/*/Cargo.toml")):
        gha.run(["soldr", "cargo", "fmt", "--manifest-path", manifest, "--all", "--", "--check"])
    return 0


def desktop_status(full: bool) -> int:
    if full:
        gha.run(["soldr", "cargo", "test", "--locked", "--all-features", "--lib", "system_tray"])
        gha.run(["soldr", "cargo", "test", "--locked", "--all-features", "--test", "terminal", "system_tray"])
    else:
        gha.run(
            [
                "soldr",
                "cargo",
                "test",
                "--locked",
                "--no-default-features",
                "--features",
                "system-tray",
                "--lib",
                "system_tray",
            ]
        )
        gha.run(
            [
                "soldr",
                "cargo",
                "test",
                "--locked",
                "--no-default-features",
                "--features",
                "system-tray",
                "--test",
                "terminal",
                "system_tray",
            ]
        )
    return 0


def release_tooling() -> int:
    gha.run([*UV, "ci/check_compilation_boundary_dependencies.py"])
    gha.run([*UV, "ci/test_release_package_features.py"])
    # tomllib needs Python >= 3.11, so these cannot share the package's 3.10
    # floor; 3.12 is the runner's own interpreter.
    gha.run([*UV, "--python", "3.12", "ci/test_auto_release.py"])
    gha.run([*UV, "--python", "3.12", "ci/test_crate_release.py"])
    return 0


def python_package() -> int:
    # Python 3.10 only: the floor AGENTS.md supports; the package has no
    # version-specific code for a second interpreter to catch.
    gha.run([*UV, "--python", "3.10", "--with-editable", ".", "--with", "pytest", "pytest", "python/tests"])
    gha.run(["uv", "build", "--clear"])
    gha.run([*UV, "--python", "3.10", "--with", "twine", "twine", "check", *sorted(glob.glob("dist/*"))])
    return 0


def abi_generation() -> int:
    gha.run([sys.executable, "ci/check_wasm_abi_generation.py"])
    gha.run(["soldr", "cargo", "test", "--locked", "--manifest-path", "tools/wasm-abi-generator/Cargo.toml"])
    return 0


def feature_isolation() -> int:
    failed = []
    for feature in ISOLATED_FEATURES:
        gha.group(feature)
        code = gha.run(
            ["soldr", "cargo", "check", "--locked", "--no-default-features", "--features", feature], check=False
        )
        if code != 0:
            failed.append(feature)
        gha.endgroup()
    if failed:
        raise gha.StepError(f"features that do not compile in isolation: {' '.join(failed)}")
    return 0


def external_consumers() -> int:
    gha.run(["soldr", "cargo", "check", "--locked", "--manifest-path", "tests/daemon-registration-consumer/Cargo.toml"])
    gha.run(
        ["soldr", "cargo", "check", "--locked", "--manifest-path", "tests/daemon-registration-v2-consumer/Cargo.toml"]
    )
    # kernal-api as both a dependency and a build-dependency; its feature
    # split is asserted by check_compilation_boundary_dependencies.py.
    gha.run(["soldr", "cargo", "check", "--locked", "--manifest-path", "tests/build-resources-consumer/Cargo.toml"])
    return 0


def popup_proof() -> None:
    """Click the external-content proof window once it appears.

    Runs inside one `dbus-run-session -- xvfb-run` so the smoke binary and
    xdotool share the display.
    """
    child = (
        "import subprocess, sys, time\n"
        f"proof = subprocess.Popen({[*SMOKE, 'popup']!r})\n"
        "window = ''\n"
        "for _ in range(50):\n"
        f"    with subprocess.Popen(['xdotool', 'search', '--name', {POPUP_WINDOW!r}],\n"
        "                          stdout=subprocess.PIPE, text=True) as search:\n"
        "        found = [word for line in search.stdout for word in line.split()]\n"
        "    window = found[0] if found else ''\n"
        "    if window:\n"
        "        break\n"
        "    time.sleep(0.1)\n"
        "if not window:\n"
        "    proof.kill()\n"
        "    sys.exit('the external-content proof window never appeared')\n"
        "time.sleep(1)\n"
        "subprocess.run(['xdotool', 'mousemove', '--window', window, '150', '150', 'click', '1'], check=True)\n"
        "sys.exit(proof.wait())\n"
    )
    gha.run([*UNDER_DISPLAY, sys.executable, "-c", child])


def webview_proofs() -> int:
    """Real semantic webview lifecycle and capture proofs under Xvfb.

    A unit test does not own process main on every harness, while Tauri needs
    its event loop there, so these run the smoke binary.
    """
    for mode in SMOKE_MODES_BEFORE_POPUP:
        gha.run([*UNDER_DISPLAY, *SMOKE, mode])
    popup_proof()
    for mode in SMOKE_MODES_AFTER_POPUP:
        gha.run([*UNDER_DISPLAY, *SMOKE, mode])
    gha.run(
        [
            *UNDER_DISPLAY,
            "soldr",
            "cargo",
            "test",
            "--locked",
            "--all-features",
            "--lib",
            "live_webkit_viewport_png_completes_capture_operation",
            "--",
            "--ignored",
            "--test-threads=1",
        ]
    )
    return 0


def screenshot_test_executable() -> str:
    """The one wasm_tauri_screenshot test executable an all-features build makes."""
    output = gha.read(
        [
            "soldr",
            "cargo",
            "test",
            "--locked",
            "--all-features",
            "--test",
            "wasm_tauri_screenshot",
            "--no-run",
            "--message-format=json",
        ]
    )
    if output.returncode != 0:
        raise gha.StepError(f"building the screenshot test exited {output.returncode}")
    executables = []
    for line in output.lines:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            message.get("reason") == "compiler-artifact"
            and message.get("target", {}).get("name") == "wasm_tauri_screenshot"
            and message.get("profile", {}).get("test") is True
            and message.get("executable")
        ):
            executables.append(message["executable"])
    if len(executables) != 1:
        raise gha.StepError("expected exactly one screenshot test executable")
    return executables[0]


def screenshot_cli() -> int:
    """Build, admit and run the real guest through the default screenshot CLI.

    The CLI starts Soldr itself. Finish the outer Soldr invocation before
    running the harness; never disable its reentry guard.
    """
    executable = screenshot_test_executable()
    log = gha.runner_temp() / "screenshot-cli.log"
    result = gha.read(
        [*UNDER_DISPLAY, executable, SCREENSHOT_CLI_TEST, "--exact", "--ignored", "--nocapture", "--test-threads=1"],
        echo=True,
        merge_stderr=True,
    )
    log.write_text(result.text + "\n", encoding="utf-8")
    # As before (`| tee` without pipefail), the summary line is the verdict.
    if not any(ONE_PASSED in line for line in result.lines):
        raise gha.StepError(f"the screenshot CLI proof did not report {ONE_PASSED!r}")
    return 0


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ci.yml linux job steps")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        sub.add_parser(name)
    sub.add_parser("desktop-status").add_argument("--full", action="store_true")
    return parser.parse_args(argv)


COMMANDS = {
    "ci-guards": ci_guards,
    "boundary-scan": boundary_scan,
    "dylint-fmt": dylint_fmt,
    "release-tooling": release_tooling,
    "python-package": python_package,
    "abi-generation": abi_generation,
    "feature-isolation": feature_isolation,
    "external-consumers": external_consumers,
    "webview-proofs": webview_proofs,
    "screenshot-cli": screenshot_cli,
}


def main() -> int:
    args = parse(sys.argv[1:])
    if args.command == "desktop-status":
        return desktop_status(args.full)
    return COMMANDS[args.command]()


if __name__ == "__main__":
    gha.main_guard(main)
