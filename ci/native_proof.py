"""Compile the native Wasm proofs on Linux; execute them on each native host.

The six-host proofs used to compile Wasmtime, Tauri and the Component tools on
every runner, holding 10x-billed Apple and 2x-billed Windows hardware for the
whole cold build. The work is now split three ways:

``guests``  builds the target-independent Wasm guests once, on Linux.
``build``   compiles every native proof binary for one target on a Linux runner
            (through Soldr's cross toolchain for Apple and Windows) and packs
            them with a manifest.
``run``     unpacks one target's bundle on that native host and executes the
            proofs. It never invokes Soldr, Cargo or a compiler.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
THREADS = "wasm32-wasip1-threads"
UNKNOWN = "wasm32-unknown-unknown"
# WebKitGTK must link the runner's own ABI, so Linux targets build natively.
LINUX_TARGETS = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")
TARGETS = LINUX_TARGETS + (
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
)

COMPILER_TEST = "wasm::compiler_dispatch::tests::compiler_actual_guest_spawns_drains_hashes_persists_and_waits"
COMPONENT_TESTS = ("wasm::component_compiler::tests::actual_guest_spawns_drains_hashes_waits_and_closes",)
COMPONENT_ENCODER_OUTPUT = (
    "validated component: {bytes} bytes; two kernel imports; not executed\n"
    "Wasmtime 45 component compilation passed; not instantiated or executed\n"
)
THREADED_TEST = "supplied_threaded_artifact_admits_and_executes_the_public_profile"
# Without --module the default CLI builds and admits its own guest through
# Soldr, which compiles. CI's screenshot-cli-guest-build job runs it on Linux;
# the native hosts skip it and run every other screenshot proof.
GUEST_BUILDING_SCREENSHOT_TEST = "default_screenshot_cli_uses_containment_and_commits_output"
PARENT_DEATH_TEST = "failure_proof::d4_parent_death_kills_exact_worker"


# Two `cargo test --no-run` feature graphs yield every proof executable.
#
# `all` is the graph the full test suite and the test archive already build,
# so on a builder that has run them it costs nothing: `--all-features` in the
# `linux` job, and the target graph (every feature but the host-only ones; see
# ci/target_features.py) that the per-target `build` job exports as
# KERNAL_TARGET_FEATURES.
# `production` is the one graph whose distinctness is the proof: the
# containment proofs run the worker binary a production build ships, without
# `wasm-sketch-worker-test-support` hooks or `tauri-webview`, and the
# admission proof shows a guest cannot reach the webview when the webview is
# not compiled in at all.
TARGET_FEATURES = os.environ.get("KERNAL_TARGET_FEATURES", "")
ALL_FEATURES = ("--features", TARGET_FEATURES) if TARGET_FEATURES else ("--all-features",)
GRAPHS = {
    "all": (*ALL_FEATURES, "--lib", "--test", "wasm_tauri_screenshot", "--test", "wasm_worker_containment"),
    "production": (
        "--features",
        "wasm-sketch-worker",
        "--test",
        "wasm_tauri_screenshot",
        "--test",
        "wasm_worker_containment",
    ),
}


@dataclass(frozen=True)
class Role:
    """One proof harness, and the binaries it drives, from one feature graph."""

    name: str
    graph: str
    test_target: str
    bins: tuple[str, ...] = ()


ROLES = (
    # The library harness also runs the threaded artifact admission test.
    Role("compiler-host", "all", "kernal_api"),
    Role("component-host", "all", "kernal_api"),
    Role("screenshot", "all", "wasm_tauri_screenshot", ("kernal-wasm-worker", "kernal-api-wasm-tauri")),
    Role("screenshot-admission", "production", "wasm_tauri_screenshot"),
    Role("worker-containment", "production", "wasm_worker_containment", ("kernal-wasm-worker",)),
    Role("worker-containment-support", "all", "wasm_worker_containment", ("kernal-wasm-worker",)),
)

GUESTS = {
    "compiler": "compiler-guest.admitted.wasm",
    "component-core": "component-core.wasm",
    "threaded": "threaded-smoke.admitted.wasm",
    "screenshot": "screenshot.admitted.wasm",
    "screenshot-trap": "screenshot-trap.admitted.wasm",
    "screenshot-block": "screenshot-block.admitted.wasm",
}
MANIFEST = "manifest.json"
RESULT = re.compile(r"^test result: ok\. (\d+) passed; 0 failed;", re.MULTILINE)


def capture(arguments: list, *, env: dict[str, str] | None = None, cwd: Path = REPO) -> str:
    """Run a command and return its stdout; stderr streams to the log."""
    arguments = [str(argument) for argument in arguments]
    print("Running: " + " ".join(arguments), flush=True)
    try:
        return subprocess.run(arguments, cwd=cwd, env=env, check=True, text=True, stdout=subprocess.PIPE).stdout
    except subprocess.CalledProcessError as error:
        print(error.stdout or "", end="", flush=True)
        raise


def stream(arguments: list, *, env: dict[str, str] | None = None, cwd: Path = REPO) -> str:
    """Run a proof, echoing its combined output live, and return that output."""
    arguments = [str(argument) for argument in arguments]
    print("Running: " + " ".join(arguments), flush=True)
    process = subprocess.Popen(
        arguments,
        cwd=cwd,
        env=env,
        text=True,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    lines = []
    for line in process.stdout:
        sys.stdout.write(line)
        lines.append(line)
    sys.stdout.flush()
    output = "".join(lines)
    if process.wait():
        raise subprocess.CalledProcessError(process.returncode, arguments, output)
    return output


def artifacts(messages: str):
    # Soldr and build scripts may interleave human-readable lines.
    for line in messages.splitlines():
        if line.startswith("{"):
            message = json.loads(line)
            if message.get("reason") == "compiler-artifact":
                yield message


def select_executable(messages: str, name: str, *, test: bool, kind: str | None = None) -> Path:
    found = [
        Path(message["executable"])
        for message in artifacts(messages)
        if message["target"]["name"] == name
        and message["profile"]["test"] == test
        and message.get("executable")
        and (kind is None or kind in message["target"].get("kind", []))
    ]
    if len(found) != 1 or not found[0].is_absolute():
        raise ValueError(f"expected exactly one absolute executable for {name}")
    return found[0]


def artifact_file(messages: str, name: str, suffix: str) -> Path:
    found = [
        Path(filename)
        for message in artifacts(messages)
        if message["target"]["name"] == name
        for filename in message.get("filenames", [])
        if filename.endswith(suffix)
    ]
    if len(found) != 1 or not found[0].is_absolute():
        raise ValueError(f"expected exactly one absolute {suffix} artifact for {name}")
    return found[0]


def rust_host() -> str:
    for line in capture(["soldr", "rustc", "-vV"]).splitlines():
        if line.startswith("host: "):
            return line[len("host: ") :].strip()
    raise ValueError("soldr rustc -vV did not report a host")


def host_triple(system: str, machine: str, *, processor_identifier: str = "", translated: bool = False) -> str:
    """The Rust triple of the hardware, not of a possibly emulated interpreter."""
    machine = machine.lower()
    if system == "Windows" and processor_identifier.upper().startswith("ARM"):
        machine = "arm64"
    if system == "Darwin" and translated:
        machine = "arm64"
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    suffix = {"Linux": "unknown-linux-gnu", "Darwin": "apple-darwin", "Windows": "pc-windows-msvc"}.get(system)
    if arch is None or suffix is None:
        raise ValueError(f"unsupported proof host {system}/{machine}")
    return f"{arch}-{suffix}"


def detect_host_triple() -> str:
    system = platform.system()
    translated = False
    if system == "Darwin":
        probe = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], text=True, capture_output=True, check=False)
        translated = probe.stdout.strip() == "1"
    return host_triple(
        system,
        platform.machine(),
        processor_identifier=os.environ.get("PROCESSOR_IDENTIFIER", ""),
        translated=translated,
    )


def pack(staging: Path, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w") as tar:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(staging).as_posix())


def stage(executable: Path, bundle: Path, role: str) -> str:
    destination = bundle / role / executable.name
    if destination.exists():
        raise ValueError(f"{role} would stage {executable.name} twice")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Copy immediately: the next feature graph rewrites Cargo's uplifted bins.
    shutil.copy2(executable, destination)
    return destination.relative_to(bundle).as_posix()


def build(target: str, archive: Path, work: Path) -> None:
    if target not in TARGETS:
        raise ValueError(f"unsupported proof target {target}")
    host = rust_host()
    cross = target != host
    if cross and target in LINUX_TARGETS:
        raise ValueError(f"{target} must build on its own architecture, not {host}")
    target_arguments = ["--target", target] if cross else []
    bundle = work / "bundle"
    if bundle.exists():
        raise ValueError("bundle staging directory already exists; refusing stale binaries")
    manifest: dict = {"target": target, "roles": {}}
    staged: dict[tuple[str, Path], str] = {}

    def stage_once(graph: str, executable: Path) -> str:
        # Roles sharing a graph share its executables; ship each one once.
        key = (graph, executable)
        if key not in staged:
            staged[key] = stage(executable, bundle, graph)
        return staged[key]

    for graph, cargo in GRAPHS.items():
        messages = capture(
            ["soldr", "cargo", "test", "--locked", "--no-run", "--message-format=json", *target_arguments, *cargo]
        )
        for role in (role for role in ROLES if role.graph == graph):
            manifest["roles"][role.name] = {
                "test": stage_once(graph, select_executable(messages, role.test_target, test=True)),
                "bins": {
                    binary: stage_once(graph, select_executable(messages, binary, test=False, kind="bin"))
                    for binary in role.bins
                },
            }
    tools = capture(
        [
            "soldr",
            "cargo",
            "build",
            "--locked",
            "--release",
            "--message-format=json",
            "--manifest-path",
            "benchmarks/wasm-sketch/component-tools/Cargo.toml",
            "--features",
            "engine-probe",
            *target_arguments,
        ]
    )
    manifest["component_tools"] = stage(
        select_executable(tools, "kernal-component-tools", test=False), bundle, "component-tools"
    )
    (bundle / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    pack(bundle, archive)


def ensure_wasm_target(target: str) -> None:
    """Materialize a Wasm target through the shared rustup repair helper."""
    capture([sys.executable, "ci/rust_target.py", target])


def embed_threaded_metadata(built: Path, admitted: Path) -> None:
    # Keep Cargo's output pristine; only the admitted copy carries metadata.
    shutil.copyfile(built, admitted)
    capture(
        [
            "soldr",
            "cargo",
            "run",
            "--locked",
            "--manifest-path",
            "tools/wasm-abi-generator/Cargo.toml",
            "--",
            "--embed-threaded-metadata",
            admitted,
        ]
    )


def guests(out: Path, work: Path) -> None:
    if out.exists() and any(out.iterdir()):
        raise ValueError("guest output directory is not empty; refusing stale guests")
    out.mkdir(parents=True, exist_ok=True)
    for target in (THREADS, UNKNOWN):
        ensure_wasm_target(target)
    guest_env = dict(os.environ, SOLDR_LINKER="default")
    compiler = capture(
        [
            "soldr",
            "cargo",
            "build",
            "--locked",
            "--manifest-path",
            "benchmarks/wasm-sketch/compiler-guest/Cargo.toml",
            "--features",
            "guest-proof",
            "--bin",
            "kernal-compiler-guest-proof",
            "--target",
            THREADS,
            "--release",
            "-j1",
            "--target-dir",
            work / "compiler-guest",
            "--message-format=json",
        ],
        env=guest_env,
    )
    embed_threaded_metadata(
        select_executable(compiler, "kernal-compiler-guest-proof", test=False), out / GUESTS["compiler"]
    )
    component = capture(
        [
            "soldr",
            "cargo",
            "build",
            "--locked",
            "--manifest-path",
            "benchmarks/wasm-sketch/component-guest/Cargo.toml",
            "--features",
            "compiler-proof",
            "--target",
            UNKNOWN,
            "--release",
            "-j1",
            "--target-dir",
            work / "component-guest",
            "--message-format=json",
        ],
        env=guest_env,
    )
    shutil.copyfile(artifact_file(component, "kernal_component_probe", ".wasm"), out / GUESTS["component-core"])
    threaded = capture(
        [
            "soldr",
            "cargo",
            "build",
            "--locked",
            "--manifest-path",
            "Cargo.toml",
            "--target",
            THREADS,
            "--release",
            "--target-dir",
            work / "threaded-smoke",
            "--message-format=json",
        ],
        env=guest_env,
        cwd=REPO / "guests" / "threaded-smoke",
    )
    embed_threaded_metadata(
        select_executable(threaded, "kernal-api-threaded-smoke", test=False), out / GUESTS["threaded"]
    )
    screenshot_env = dict(os.environ, CARGO_TARGET_DIR=str(work / "screenshot"))
    for key, flags in (
        ("screenshot", []),
        ("screenshot-trap", ["--trap-after-capture"]),
        ("screenshot-block", ["--block-after-capture"]),
    ):
        printed = (
            capture([sys.executable, "examples/wasm-tauri-screenshot/build_guest.py", *flags], env=screenshot_env)
            .strip()
            .splitlines()
        )
        admitted = Path(printed[-1]) if printed else Path()
        if not admitted.is_absolute() or not admitted.is_file():
            raise ValueError(f"{key} guest build did not print an admitted artifact")
        shutil.copyfile(admitted, out / GUESTS[key])


def unpack(native_target: str, bundle_archive: Path, guest_dir: Path, work: Path) -> tuple[Path, dict]:
    host = detect_host_triple()
    if host != native_target:
        raise ValueError(f"this runner is {host}, not native {native_target}")
    if work.exists() and any(work.iterdir()):
        raise ValueError("work directory is not empty; refusing stale proof state")
    root = work / "bundle"
    root.mkdir(parents=True)
    with tarfile.open(bundle_archive) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(root, filter="data")
        else:
            tar.extractall(root)
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if manifest.get("target") != native_target:
        raise ValueError(f"bundle was built for {manifest.get('target')}, not {native_target}")
    for name in GUESTS.values():
        if not (guest_dir / name).is_file():
            raise ValueError(f"missing guest artifact {name}")
    return root, manifest


def role_test(root: Path, manifest: dict, role: str) -> Path:
    return root / manifest["roles"][role]["test"]


def role_env(root: Path, manifest: dict, role: str, base: dict[str, str]) -> dict[str, str]:
    # The tests read `NEXTEST_BIN_EXE_*` at run time because `CARGO_BIN_EXE_*`
    # is a compile-time path on the Linux builder.
    env = dict(base)
    for binary, relative in manifest["roles"][role]["bins"].items():
        env[f"NEXTEST_BIN_EXE_{binary}"] = str(root / relative)
    return env


def require_passed(output: str, what: str, *, exactly: int | None = None) -> None:
    passed = sum(int(count) for count in RESULT.findall(output))
    if exactly is not None and passed != exactly:
        raise ValueError(f"{what} did not execute exactly once" if exactly == 1 else f"{what} ran {passed} tests")
    if passed == 0:
        raise ValueError(f"{what} executed no tests")


def require_listed(harness: Path, test: str, *, ignored: bool, env: dict[str, str] | None = None) -> None:
    # A test filter that matches nothing still succeeds; prove registration.
    listing = capture([harness, "--list", *(["--ignored"] if ignored else [])], env=env)
    if f"{test}: test" not in listing.splitlines():
        raise ValueError(f"{test} is missing from the native test harness")


def with_display(arguments: list, env: dict[str, str]) -> list:
    if platform.system() != "Linux":
        return list(arguments)
    # xvfb-run is a /bin/sh script, and dash drops environment variables whose
    # names are not shell identifiers -- every NEXTEST_BIN_EXE_<name-with-dash>
    # (PR #271's first run). Hand them to `env` as arguments after the wrapper.
    assignments = [f"{name}={value}" for name, value in sorted(env.items()) if name.startswith("NEXTEST_BIN_EXE_")]
    return ["dbus-run-session", "--", "xvfb-run", "-a", "env", *assignments, *arguments]


def run_compiler(root: Path, manifest: dict, guest_dir: Path, work: Path) -> None:
    harness = role_test(root, manifest, "compiler-host")
    require_listed(harness, COMPILER_TEST, ignored=True)
    output = stream(
        [harness, COMPILER_TEST, "--exact", "--ignored", "--nocapture"],
        env=dict(os.environ, KERNAL_COMPILER_GUEST_WASM=str(guest_dir / GUESTS["compiler"])),
    )
    require_passed(output, "the compiler cache proof", exactly=1)

    component = work / "compiler-policy.component.wasm"
    if component.exists():
        raise ValueError("component proof output already exists; refusing a stale artifact")
    encoding = capture([root / manifest["component_tools"], guest_dir / GUESTS["component-core"], component])
    if encoding != COMPONENT_ENCODER_OUTPUT.format(bytes=component.stat().st_size):
        raise ValueError("Component encoder did not structurally validate and engine-compile the artifact")
    component_harness = role_test(root, manifest, "component-host")
    for test in COMPONENT_TESTS:
        require_listed(component_harness, test, ignored=True)
        output = stream(
            [component_harness, test, "--exact", "--ignored", "--nocapture"],
            env=dict(os.environ, KERNAL_COMPONENT_COMPILER_WASM=str(component)),
        )
        require_passed(output, f"Component compiler proof {test}", exactly=1)


def run_screenshot(root: Path, manifest: dict, guest_dir: Path, work: Path) -> None:
    base = dict(
        os.environ,
        KERNAL_API_SCREENSHOT_ARTIFACT_WASM=str(guest_dir / GUESTS["screenshot"]),
        KERNAL_API_SCREENSHOT_TRAP_ARTIFACT_WASM=str(guest_dir / GUESTS["screenshot-trap"]),
        KERNAL_API_SCREENSHOT_BLOCK_ARTIFACT_WASM=str(guest_dir / GUESTS["screenshot-block"]),
        KERNAL_API_THREADED_ARTIFACT_WASM=str(guest_dir / GUESTS["threaded"]),
    )
    screenshot = role_test(root, manifest, "screenshot")
    screenshot_env = role_env(root, manifest, "screenshot", base)
    stream([screenshot], env=screenshot_env)
    output = stream(
        with_display(
            [screenshot, "--ignored", "--nocapture", "--test-threads=1", "--skip", GUEST_BUILDING_SCREENSHOT_TEST],
            screenshot_env,
        ),
        env=screenshot_env,
    )
    require_passed(output, "the native screenshot proof")
    # The host-only graph admits the same guests without the worker binary.
    # `--nocapture` matches every other proof invocation: without it libtest
    # buffers the harness output, so a hung test dies into the step's timeout
    # with zero lines logged (run 35702323046 lost 27 minutes that way).
    admission_env = role_env(root, manifest, "screenshot-admission", base)
    stream(
        with_display(
            [role_test(root, manifest, "screenshot-admission"), "--ignored", "--nocapture", "--test-threads=1"],
            admission_env,
        ),
        env=admission_env,
    )

    output = stream([role_test(root, manifest, "compiler-host"), THREADED_TEST], env=base)
    require_passed(output, "the threaded artifact admission proof", exactly=1)
    output = stream(
        [
            role_test(root, manifest, "worker-containment"),
            "cargo_built_threaded_guest_",
            "--ignored",
            "--test-threads=1",
        ],
        env=role_env(root, manifest, "worker-containment", base),
    )
    require_passed(output, "threaded worker containment")
    support = role_test(root, manifest, "worker-containment-support")
    support_env = role_env(root, manifest, "worker-containment-support", base)
    output = stream([support, "cargo_built_threaded_guest_forced_output_cleanup", "--ignored"], env=support_env)
    require_passed(output, "forced output cleanup")
    # An external process-lifecycle proof: the worker must die with its parent.
    require_listed(support, PARENT_DEATH_TEST, ignored=False, env=support_env)
    output = stream([support, PARENT_DEATH_TEST, "--exact", "--test-threads=1"], env=support_env)
    require_passed(output, "parent-death containment", exactly=1)


SUITES = {"compiler": run_compiler, "screenshot": run_screenshot}


def run_suite(suite: str, native_target: str, bundle_archive: Path, guest_dir: Path, work: Path) -> None:
    root, manifest = unpack(native_target, bundle_archive, guest_dir, work)
    SUITES[suite](root, manifest, guest_dir, work)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    guest_parser = commands.add_parser("guests")
    guest_parser.add_argument("--out", type=Path, required=True)
    guest_parser.add_argument("--work", type=Path, required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("--target", required=True, choices=TARGETS)
    build_parser.add_argument("--archive", type=Path, required=True)
    build_parser.add_argument("--work", type=Path, required=True)
    target_parser = commands.add_parser(
        "wasm-target",
        help="Materialize a Wasm target, verifying it on disk rather than trusting rustup",
    )
    target_parser.add_argument("target", nargs="?", default=THREADS)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("suite", choices=tuple(SUITES))
    run_parser.add_argument("--native-target", required=True, choices=TARGETS)
    run_parser.add_argument("--bundle", type=Path, required=True)
    run_parser.add_argument("--guests", type=Path, required=True)
    run_parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    for name in ("out", "work", "archive", "bundle", "guests"):
        value = getattr(args, name, None)
        if value is not None and not value.is_absolute():
            parser.error(f"--{name} must be absolute caller-owned storage")
    if args.command == "guests":
        guests(args.out, args.work)
    elif args.command == "wasm-target":
        ensure_wasm_target(args.target)
    elif args.command == "build":
        build(args.target, args.archive, args.work)
    else:
        run_suite(args.suite, args.native_target, args.bundle, args.guests, args.work)


if __name__ == "__main__":
    main()
