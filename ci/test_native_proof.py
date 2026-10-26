"""Fail-closed tests for the Linux-built, natively executed proof runner."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ci import native_proof as proof

PASSED_ONCE = "test result: ok. 1 passed; 0 failed; 0 ignored;\n"


def compiler_artifact(name, path, *, test=False, kind="bin"):
    return json.dumps(
        {
            "reason": "compiler-artifact",
            "target": {"name": name, "kind": [kind]},
            "profile": {"test": test},
            "executable": str(path),
            "filenames": [str(path)],
        }
    )


def temporary_directory(case):
    path = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, path, True)
    return path


class FakeCargo:
    """Answers Soldr invocations with artifacts that exist on disk."""

    def __init__(self, root, host):
        self.root = root
        self.host = host
        self.calls = []

    def __call__(self, arguments, *, env=None, cwd=proof.REPO):
        arguments = [str(argument) for argument in arguments]
        self.calls.append(arguments)
        if arguments[:3] == ["soldr", "rustc", "-vV"]:
            return f"release: 1.95.0\nhost: {self.host}\n"
        out = self.root / f"call-{len(self.calls)}"
        out.mkdir(parents=True)

        def executable(name):
            path = out / name
            path.write_text(name)
            return path

        lines = ["not a Cargo message"]
        if "benchmarks/wasm-sketch/component-tools/Cargo.toml" in arguments:
            lines.append(compiler_artifact("kernal-component-tools", executable("kernal-component-tools")))
            return "\n".join(lines)
        graph = next(name for name, cargo in proof.GRAPHS.items() if arguments[-len(cargo):] == list(cargo))
        roles = [role for role in proof.ROLES if role.graph == graph]
        # The library itself is never selected as a test harness.
        lines.append(json.dumps({"reason": "compiler-artifact", "target": {"name": "kernal_api", "kind": ["lib"]}, "profile": {"test": False}, "executable": None}))
        # Cargo reports each unit once per graph, however many roles use it.
        for test_target in dict.fromkeys(role.test_target for role in roles):
            lines.append(compiler_artifact(test_target, executable(f"{test_target}-0123"), test=True, kind="test"))
        for binary in dict.fromkeys(binary for role in roles for binary in role.bins):
            lines.append(compiler_artifact(binary, executable(binary)))
        return "\n".join(lines)


def make_bundle(root, target):
    staging = root / "staging"
    manifest = {"target": target, "roles": {}, "component_tools": "component-tools/kernal-component-tools"}
    files = [manifest["component_tools"]]
    for role in proof.ROLES:
        entry = {"test": f"{role.name}/{role.test_target}", "bins": {binary: f"{role.name}/{binary}" for binary in role.bins}}
        manifest["roles"][role.name] = entry
        files += [entry["test"], *entry["bins"].values()]
    for relative in files:
        (staging / relative).parent.mkdir(parents=True, exist_ok=True)
        (staging / relative).write_text(relative)
    (staging / proof.MANIFEST).write_text(json.dumps(manifest))
    archive = root / "bundle.tar"
    proof.pack(staging, archive)
    guest_dir = root / "guests"
    guest_dir.mkdir()
    for name in proof.GUESTS.values():
        (guest_dir / name).write_bytes(b"\0asm")
    return archive, guest_dir


class FakeHost:
    """A native host whose harnesses list every proof and pass once."""

    def __init__(self, result=PASSED_ONCE):
        self.result = result
        self.calls = []

    def capture(self, arguments, *, env=None, cwd=proof.REPO):
        arguments = [str(argument) for argument in arguments]
        self.calls.append((arguments, env))
        if "--list" in arguments:
            tests = (proof.COMPILER_TEST, *proof.COMPONENT_TESTS, proof.PARENT_DEATH_TEST)
            return "".join(f"{test}: test\n" for test in tests)
        if arguments[0].endswith("kernal-component-tools"):
            Path(arguments[2]).write_bytes(b"c" * 17)
            return proof.COMPONENT_ENCODER_OUTPUT.format(bytes=17)
        raise AssertionError(f"unexpected capture: {arguments}")

    def stream(self, arguments, *, env=None, cwd=proof.REPO):
        self.calls.append(([str(argument) for argument in arguments], env))
        return self.result


class HostTripleTests(unittest.TestCase):
    def test_maps_every_native_proof_host(self):
        cases = {
            ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
            ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
            ("Darwin", "x86_64"): "x86_64-apple-darwin",
            ("Darwin", "arm64"): "aarch64-apple-darwin",
            ("Windows", "AMD64"): "x86_64-pc-windows-msvc",
            ("Windows", "ARM64"): "aarch64-pc-windows-msvc",
        }
        for (system, machine), triple in cases.items():
            self.assertEqual(proof.host_triple(system, machine), triple)
        self.assertEqual(set(cases.values()), set(proof.TARGETS))

    def test_sees_through_an_emulated_interpreter(self):
        self.assertEqual(
            proof.host_triple("Windows", "AMD64", processor_identifier="ARMv8 (64-bit) Family 8"),
            "aarch64-pc-windows-msvc",
        )
        self.assertEqual(proof.host_triple("Darwin", "x86_64", translated=True), "aarch64-apple-darwin")
        with self.assertRaises(ValueError):
            proof.host_triple("FreeBSD", "amd64")


class SelectionTests(unittest.TestCase):
    def test_executable_selection_is_fail_closed(self):
        worker = Path.cwd() / "worker"
        one = compiler_artifact("worker", worker)
        self.assertEqual(proof.select_executable(one, "worker", test=False, kind="bin"), worker)
        for messages, test in ((one + "\n" + one, False), (one, True), (compiler_artifact("worker", "relative/worker"), False)):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                proof.select_executable(messages, "worker", test=test)


class BuildTests(unittest.TestCase):
    def build(self, target, host):
        root = temporary_directory(self)
        fake = FakeCargo(root / "cargo", host)
        archive = root / "out" / "bundle.tar"
        with patch.object(proof, "capture", side_effect=fake):
            proof.build(target, archive, root / "work")
        extracted = root / "extracted"
        with proof.tarfile.open(archive) as tar:
            tar.extractall(extracted)
        manifest = json.loads((extracted / proof.MANIFEST).read_text())
        return [call for call in fake.calls if call[:2] == ["soldr", "cargo"]], manifest, extracted

    def test_cross_build_compiles_each_graph_once_for_the_target_and_packs_it(self):
        cargo, manifest, extracted = self.build("aarch64-pc-windows-msvc", "x86_64-unknown-linux-gnu")
        # One compile per feature graph plus the Component tools, not one per role.
        self.assertEqual(len(cargo), len(proof.GRAPHS) + 1)
        for command in cargo:
            self.assertIn("--locked", command)
            self.assertNotIn("--no-cache", command)
            self.assertEqual(command[command.index("--target") + 1], "aarch64-pc-windows-msvc")
        for command in cargo[:-1]:
            self.assertIn("--no-run", command)
        self.assertEqual(manifest["target"], "aarch64-pc-windows-msvc")
        self.assertEqual(set(manifest["roles"]), {role.name for role in proof.ROLES})
        self.assertEqual(set(manifest["roles"]["screenshot"]["bins"]), {"kernal-wasm-worker", "kernal-api-wasm-tauri"})
        staged = [manifest["component_tools"]]
        for entry in manifest["roles"].values():
            staged += [entry["test"], *entry["bins"].values()]
        for relative in staged:
            self.assertTrue((extracted / relative).is_file(), relative)
        # Each containment graph ships the worker it was compiled with.
        roles = manifest["roles"]
        self.assertNotEqual(
            roles["worker-containment"]["bins"]["kernal-wasm-worker"],
            roles["worker-containment-support"]["bins"]["kernal-wasm-worker"],
        )
        # Roles sharing a graph share its files; nothing is staged twice.
        self.assertEqual(roles["compiler-host"]["test"], roles["component-host"]["test"])
        self.assertEqual(
            roles["screenshot"]["bins"]["kernal-wasm-worker"],
            roles["worker-containment-support"]["bins"]["kernal-wasm-worker"],
        )
        self.assertEqual(sorted(path.relative_to(extracted).as_posix() for path in extracted.rglob("*") if path.is_file()),
                         sorted(set(staged) | {proof.MANIFEST}))

    def test_the_all_graph_follows_the_build_job_target_features(self):
        """In `build`, `all` is the target graph the archive built, not `--all-features`."""
        import importlib

        with patch.dict("os.environ", {"KERNAL_TARGET_FEATURES": "fs,json"}):
            reloaded = importlib.reload(proof)
            self.assertEqual(reloaded.GRAPHS["all"][:2], ("--features", "fs,json"))
            self.assertNotIn("--all-features", reloaded.GRAPHS["all"])
        importlib.reload(proof)

    def test_the_production_graph_is_the_only_one_not_shared_with_the_suite(self):
        """Every graph but `production` is the all-features one the suite builds."""
        self.assertEqual(set(proof.GRAPHS), {"all", "production"})
        # The `linux` suite's graph unless the `build` job exported its target graph.
        self.assertEqual(proof.GRAPHS["all"][0], "--all-features")
        production = proof.GRAPHS["production"]
        self.assertNotIn("--all-features", production)
        self.assertEqual(production[production.index("--features") + 1], "wasm-sketch-worker")
        # The containment and admission proofs are the production graph's reason to exist.
        by_name = {role.name: role for role in proof.ROLES}
        self.assertEqual(by_name["worker-containment"].graph, "production")
        self.assertEqual(by_name["screenshot-admission"].graph, "production")

    def test_native_linux_build_links_the_host_abi(self):
        cargo, manifest, _ = self.build("aarch64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")
        self.assertEqual(manifest["target"], "aarch64-unknown-linux-gnu")
        self.assertFalse(any("--target" in command for command in cargo))

    def test_refuses_to_cross_build_a_linux_target(self):
        root = temporary_directory(self)
        fake = FakeCargo(root, "x86_64-unknown-linux-gnu")
        with patch.object(proof, "capture", side_effect=fake), self.assertRaisesRegex(ValueError, "own architecture"):
            proof.build("aarch64-unknown-linux-gnu", root / "bundle.tar", root / "work")
        self.assertEqual(fake.calls, [["soldr", "rustc", "-vV"]])


class RunTests(unittest.TestCase):
    target = "x86_64-apple-darwin"

    def run_suite(self, suite, host, *, system="Darwin", detected=None):
        root = temporary_directory(self)
        archive, guest_dir = make_bundle(root, self.target)
        work = root / "work"
        with (
            patch.object(proof, "detect_host_triple", return_value=detected or self.target),
            patch.object(proof.platform, "system", return_value=system),
            patch.object(proof, "capture", side_effect=host.capture),
            patch.object(proof, "stream", side_effect=host.stream),
        ):
            proof.run_suite(suite, self.target, archive, guest_dir, work)
        return work

    def test_compiler_suite_executes_prebuilt_proofs_without_a_toolchain(self):
        host = FakeHost()
        work = self.run_suite("compiler", host)
        commands = [arguments for arguments, _ in host.calls]
        self.assertFalse(any(command[0] in {"soldr", "cargo", "rustc"} for command in commands))
        core = next(env for arguments, env in host.calls if proof.COMPILER_TEST in arguments and "--list" not in arguments)
        self.assertTrue(core["KERNAL_COMPILER_GUEST_WASM"].endswith(proof.GUESTS["compiler"]))
        component_runs = [
            env for arguments, env in host.calls
            if "--list" not in arguments and any(test in arguments for test in proof.COMPONENT_TESTS)
        ]
        # One run per Component proof: the cache-hit proof left with the
        # compiler-artifact cache experiment.
        self.assertEqual(len(component_runs), len(proof.COMPONENT_TESTS))
        for env in component_runs:
            self.assertEqual(env["KERNAL_COMPONENT_COMPILER_WASM"], str(work / "compiler-policy.component.wasm"))

    def test_rejects_a_zero_executed_compiler_proof(self):
        host = FakeHost("test result: ok. 0 passed; 0 failed; 0 ignored;\n")
        with self.assertRaisesRegex(ValueError, "exactly once"):
            self.run_suite("compiler", host)

    def test_refuses_a_foreign_host(self):
        with self.assertRaisesRegex(ValueError, "not native"):
            self.run_suite("compiler", FakeHost(), detected="aarch64-apple-darwin")

    def test_screenshot_suite_supplies_each_graphs_own_binaries(self):
        host = FakeHost()
        self.run_suite("screenshot", host, system="Linux")
        ignored = [(arguments, env) for arguments, env in host.calls if "--ignored" in arguments and "--list" not in arguments]
        screenshot, screenshot_env = ignored[0]
        self.assertEqual(screenshot[:5], ["dbus-run-session", "--", "xvfb-run", "-a", "env"])
        self.assertTrue(screenshot_env["NEXTEST_BIN_EXE_kernal-wasm-worker"].endswith("screenshot/kernal-wasm-worker"))
        self.assertTrue(screenshot_env["NEXTEST_BIN_EXE_kernal-api-wasm-tauri"].endswith("screenshot/kernal-api-wasm-tauri"))
        # dash (xvfb-run's /bin/sh) drops hyphenated variable names, so the
        # binaries must reach the harness as `env` arguments.
        harness = next(index for index, argument in enumerate(screenshot) if argument.endswith("wasm_tauri_screenshot"))
        assignments = screenshot[5:harness]
        self.assertEqual(
            sorted(assignment.split("=", 1)[0] for assignment in assignments),
            ["NEXTEST_BIN_EXE_kernal-api-wasm-tauri", "NEXTEST_BIN_EXE_kernal-wasm-worker"],
        )
        for assignment in assignments:
            name, value = assignment.split("=", 1)
            self.assertEqual(value, screenshot_env[name])
        self.assertEqual(screenshot[screenshot.index("--skip") + 1], proof.GUEST_BUILDING_SCREENSHOT_TEST)
        # The admission phase reuses this harness without `--skip`; it must
        # stream live too, or a hang there reaches the step timeout silently.
        admission = next(
            arguments
            for arguments, _ in ignored
            if "--skip" not in arguments and any("wasm_tauri_screenshot" in argument for argument in arguments)
        )
        self.assertIn("--nocapture", admission)
        containment_env = next(env for arguments, env in host.calls if "cargo_built_threaded_guest_" in arguments)
        self.assertTrue(containment_env["NEXTEST_BIN_EXE_kernal-wasm-worker"].endswith("worker-containment/kernal-wasm-worker"))
        parent_death = [arguments for arguments, _ in host.calls if proof.PARENT_DEATH_TEST in arguments and "--list" not in arguments]
        self.assertEqual(len(parent_death), 1)
        self.assertIn("--exact", parent_death[0])

    def test_screenshot_suite_needs_no_virtual_display_off_linux(self):
        host = FakeHost()
        self.run_suite("screenshot", host, system="Windows")
        self.assertFalse(any(arguments[0] in {"dbus-run-session", "env"} for arguments, _ in host.calls))
        ignored = next(arguments for arguments, _ in host.calls if "--ignored" in arguments and "--nocapture" in arguments)
        self.assertEqual(ignored[ignored.index("--skip") + 1], proof.GUEST_BUILDING_SCREENSHOT_TEST)


class WasmTargetRepairTests(unittest.TestCase):
    def repair(
        self,
        behavior,
        *,
        healthy=False,
        toolchain=None,
        reported_libdir=None,
        reported_sysroot=None,
    ):
        root = temporary_directory(self)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        libdir = root / "lib"
        sysroot = root / "sysroot"
        (sysroot / "lib" / "rustlib").mkdir(parents=True)
        calls = root / "calls"
        effects = root / "effects"
        add_count = root / "add-count"
        effects.write_text(behavior, encoding="utf-8")
        add_count.write_text("0", encoding="utf-8")
        if healthy:
            libdir.mkdir()
            (libdir / "libcore-0.rlib").touch()
            (libdir / "libstd-0.rlib").touch()

        fake_soldr = bin_dir / "soldr"
        fake_soldr.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$CALLS"
if [ "$1" = rustc ] && [ "$2" = --print ]; then
    if [ "$3" = target-libdir ]; then printf '%s\\n' "$LIBDIR"; else printf '%s\\n' "$SYSROOT"; fi
    exit 0
fi
if [ "$1" = rustup ] && [ "$2" = run ] && [ "$4" = rustc ]; then
    if [ "$6" = target-libdir ]; then printf '%s\\n' "$LIBDIR"; else printf '%s\\n' "$SYSROOT"; fi
    exit 0
fi
if [ "$1" = rustup ] && [ "$2" = target ] && [ "$3" = add ]; then
    count=$(cat "$ADD_COUNT")
    count=$((count + 1))
    printf '%s' "$count" > "$ADD_COUNT"
    case "$(cat "$EFFECTS")" in
        immediate) mkdir -p "$LIBDIR"; touch "$LIBDIR/libcore-0.rlib" "$LIBDIR/libstd-0.rlib" ;;
        repair) if [ "$count" -eq 2 ]; then mkdir -p "$LIBDIR"; touch "$LIBDIR/libcore-0.rlib" "$LIBDIR/libstd-0.rlib"; fi ;;
    esac
fi
""",
            encoding="utf-8",
        )
        fake_soldr.chmod(0o755)
        command = [sys.executable, str(proof.REPO / "ci/rust_target.py")]
        if toolchain:
            command.append(toolchain)
        command.append(proof.THREADS)
        environment = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            CALLS=str(calls),
            LIBDIR=reported_libdir or str(libdir),
            SYSROOT=reported_sysroot or str(sysroot),
            EFFECTS=str(effects),
            ADD_COUNT=str(add_count),
        )
        result = subprocess.run(command, check=False, capture_output=True, text=True, env=environment)
        manifest = sysroot / "lib" / "rustlib" / f"manifest-rust-std-{proof.THREADS}"
        call_lines = calls.read_text(encoding="utf-8").splitlines()
        return result, call_lines, manifest

    def test_native_proof_delegates_to_the_shared_target_repair_helper(self):
        with patch.object(proof, "capture") as capture:
            proof.ensure_wasm_target(proof.THREADS)
        capture.assert_called_once_with([sys.executable, "ci/rust_target.py", proof.THREADS])

    def test_healthy_toolchain_is_not_mutated(self):
        result, calls, _ = self.repair("never", healthy=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call.startswith("rustup target") for call in calls))

    def test_missing_target_is_installed(self):
        result, calls, _ = self.repair("immediate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(call.startswith("rustup target add") for call in calls), 1)

    def test_stale_bookkeeping_is_repaired_through_an_empty_manifest(self):
        result, calls, manifest = self.repair("repair", toolchain="nightly-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(call.startswith("rustup run nightly-test rustc") for call in calls))
        self.assertEqual(
            [call.split()[2] for call in calls if call.startswith("rustup target")],
            ["add", "remove", "add"],
        )
        self.assertTrue(manifest.is_file())

    def test_unrepairable_target_fails_closed(self):
        result, _, _ = self.repair("never")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still lacks core/std", result.stderr)

    def test_relative_target_libdir_fails_closed(self):
        result, _, _ = self.repair("never", reported_libdir="relative/lib")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rustc target-libdir is not absolute", result.stderr)

    def test_relative_sysroot_fails_closed(self):
        result, _, _ = self.repair("never", reported_sysroot="relative/sysroot")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rustc sysroot is not absolute", result.stderr)


class GuestTests(unittest.TestCase):
    def test_builds_every_guest_once_with_the_cache_enabled(self):
        root = temporary_directory(self)
        out = root / "out"
        calls = []

        def capture(arguments, *, env=None, cwd=proof.REPO):
            arguments = [str(argument) for argument in arguments]
            calls.append(arguments)
            built = root / f"built-{len(calls)}.wasm"
            built.write_bytes(b"\0asm")
            if arguments[:2] == [sys.executable, "examples/wasm-tauri-screenshot/build_guest.py"]:
                return f"cargo noise\n{built}\n"
            if "--embed-threaded-metadata" in arguments:
                return ""
            name = arguments[arguments.index("--bin") + 1] if "--bin" in arguments else (
                "kernal_component_probe" if "component-guest" in " ".join(arguments) else "kernal-api-threaded-smoke"
            )
            return compiler_artifact(name, built)

        with (
            patch.object(proof, "ensure_wasm_target") as ensure,
            patch.object(proof, "capture", side_effect=capture),
        ):
            proof.guests(out, root / "work")
        self.assertEqual([call.args[0] for call in ensure.call_args_list], [proof.THREADS, proof.UNKNOWN])
        self.assertEqual(sorted(path.name for path in out.iterdir()), sorted(proof.GUESTS.values()))
        self.assertFalse(any("--no-cache" in call for call in calls))
        self.assertEqual(sum("--embed-threaded-metadata" in call for call in calls), 2)
        with self.assertRaisesRegex(ValueError, "not empty"):
            proof.guests(out, root / "work")


if __name__ == "__main__":
    unittest.main()
