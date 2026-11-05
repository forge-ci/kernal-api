"""Prove the feature graph has the dependency boundaries measured for #3.

The first command in every pair is the RED state: it must *not* find the
optional implementation.  The second is GREEN: enabling its owning feature
must find it.  `cargo tree`, rather than Cargo.lock, is intentional: a lockfile
contains every optional package and therefore cannot prove feature isolation.
Measure normal/build edges: TLS fixtures may use OpenSSL as a dev dependency
without making it part of a default consumer's compilation graph.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

CASES = (
    ("pty", "portable-pty"),
    ("text-similarity", "strsim"),
    ("command-arguments", "shell-words"),
    ("json", "serde_json"),
    ("source-cpp", "tree-sitter"),
    ("source-cpp", "tree-sitter-cpp"),
    ("wasm-sketch-host", "wasmtime"),
    ("ipc", "interprocess"),
    ("tokio-console", "console-subscriber"),
    ("allocator", "mimalloc-pprof"),
    ("fs-watch", "notify"),
    ("hash-sha256", "sha2"),
    ("http-client", "reqwest"),
    ("http-client", "hyper"),
    ("http-client", "openssl-sys"),
    ("http-client-rustls", "reqwest"),
    ("http-client-rustls", "rustls"),
    ("http-server", "hyper"),
    ("http-server", "http-body-util"),
    ("event-stream", "tokio-stream"),
    ("sqlite", "rusqlite"),
    ("archive", "zip"),
    ("archive", "tar"),
    ("archive", "zstd"),
    ("archive-xz", "lzma-rust2"),
    ("archive-bzip2", "bzip2"),
    ("archive-auth-test-support", "openssl"),
    ("tauri-webview", "tauri"),
    ("tauri-webview", "tauri-runtime-wry"),
    ("tauri-webview", "wry"),
    # The broker client adapter pulls running-process/client, whose CLI,
    # config, and IPC stack must stay out of every graph that does not ask.
    ("broker-client", "clap"),
    ("broker-client", "interprocess"),
    ("broker-client", "anyhow"),
    # The build-script helper's resource compiler is for build scripts only.
    ("build-resources", "embed-resource"),
)

# An application with kernal-api as both a dependency and a build-dependency.
BUILD_RESOURCES_CONSUMER = "tests/build-resources-consumer/Cargo.toml"

SKETCH_AND_WEBVIEW_PACKAGES = {
    "tauri",
    "tauri-runtime",
    "tauri-runtime-wry",
    "wry",
    "webkit2gtk",
    "webview2-com",
    "wasmtime",
    "wasmparser",
    "fp-bindgen",
    "fp-bindgen-support",
    "kernal-api-v1-bindings",
}


def lock_sync_failures() -> list[str]:
    """Catch release bumps before resolving any dependency graphs."""
    manifest = tomllib.loads(Path("Cargo.toml").read_text(encoding="utf-8"))["package"]
    paths = subprocess.run(
        ["git", "ls-files", "*Cargo.lock"], check=True, text=True, capture_output=True
    ).stdout.splitlines()
    failures = []
    for path in paths:
        packages = tomllib.loads(Path(path).read_text(encoding="utf-8"))["package"]
        for package in packages:
            if package["name"] == manifest["name"] and "source" not in package:
                if package["version"] != manifest["version"]:
                    failures.append(
                        f"dependent lock sync: {path} pins {manifest['name']} {package['version']}; "
                        f"Cargo.toml is {manifest['version']}; run soldr cargo update "
                        f"-p {manifest['name']} --manifest-path {Path(path).with_name('Cargo.toml')}"
                    )
    return failures


def verify_lock_sync() -> None:
    problems = lock_sync_failures()
    if problems:
        raise SystemExit("\n".join(problems))


def cargo_tree(command: list[str]) -> str:
    """Keep Cargo's diagnostic when a locked graph cannot be resolved."""
    try:
        return subprocess.run(command, check=True, text=True, capture_output=True).stdout
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            f"compilation boundary: {' '.join(command)} failed (exit {error.returncode})\n"
            f"cargo stderr:\n{error.stderr or '(empty)'}"
        ) from None


def tree(features: str, *, normal_only: bool = False) -> set[str]:
    command = [
        "soldr",
        "cargo",
        "tree",
        "--locked",
        "--no-default-features",
        "--edges",
        "normal" if normal_only else "normal,build",
        "--prefix",
        "none",
    ]
    if features:
        command.extend(("--features", features))
    stdout = cargo_tree(command)
    return {line.split(maxsplit=1)[0] for line in stdout.splitlines() if line and not line.startswith("[")}


def consumer_features(edges: str, *extra: str) -> set[str]:
    """kernal-api's resolved features on one edge kind of the fixture consumer."""
    command = [
        "soldr",
        "cargo",
        "tree",
        "--locked",
        "--manifest-path",
        BUILD_RESOURCES_CONSUMER,
        "--edges",
        edges,
        "--depth",
        "1",
        "--prefix",
        "depth",
        "--format",
        "{p}|{f}",
        *extra,
    ]
    stdout = cargo_tree(command)
    rows = [line[1:].split("|", 1) for line in stdout.splitlines() if line.startswith("1kernal-api ")]
    if len(rows) != 1:
        raise SystemExit(f"expected one kernal-api {edges} edge, found {len(rows)}")
    return {feature for feature in rows[0][1].split(",") if feature}


def build_resources_consumer_failures() -> list[str]:
    """RED/GREEN for the documented build-dependency shape.

    A capability enabled in the application's `[dependencies]` entry must not
    reach its build script, and `build-resources` must not reach the runtime.
    A `kernal-api/<feature>` entry in the application's own feature table
    does reach the build script -- Cargo applies it to every dependency with
    that name -- and the documentation says so; this pins that behaviour.
    """
    failures = []
    runtime = consumer_features("normal")
    build = consumer_features("build")
    if "window-icon" not in runtime or "build-resources" in runtime:
        failures.append(f"consumer runtime kernal-api features are {sorted(runtime)}")
    if build != {"build-resources"}:
        failures.append(f"consumer build-script kernal-api features are {sorted(build)}")
    forwarded = consumer_features("build", "--features", "forwarded")
    if "text-similarity" not in forwarded:
        failures.append(
            "a forwarded kernal-api/<feature> no longer reaches the build-dependency; "
            "update the build-resources documentation"
        )
    return failures


def rustls_client_failures() -> list[str]:
    """`http-client-rustls` exists so a consumer can ship without OpenSSL (#427)."""
    graph = tree("http-client-rustls")
    native = sorted(graph & {"openssl", "openssl-sys", "native-tls"})
    return [f"http-client-rustls graph contains {', '.join(native)}"] if native else []


def fs_scan_failures(default_graph: set[str]) -> list[str]:
    """The scan feature must activate no dependency beyond the default HAL."""
    if tree("fs-scan") != default_graph:
        return ["fs-scan unexpectedly changes the default dependency graph"]
    return []


def main() -> int:  # noqa: C901 -- Existing dependency-boundary checks; ratchet complexity 13.
    verify_lock_sync()
    default_graph = tree("")
    failures: list[str] = []
    # TOML already builds the kernel's catalog. Only its runtime edge is opt-in.
    if "toml" in tree("", normal_only=True):
        failures.append("default runtime graph unexpectedly contains toml")
    if "toml" not in tree("config-toml", normal_only=True):
        failures.append("config-toml runtime graph omits toml")
    for label, graph in (("default", default_graph), ("full", tree("full"))):
        unexpected = sorted(graph & SKETCH_AND_WEBVIEW_PACKAGES)
        if unexpected:
            failures.append(f"{label} graph unexpectedly contains {', '.join(unexpected)}")
    failures.extend(fs_scan_failures(default_graph))
    if "portable-pty" in tree("terminal-input"):
        failures.append("terminal-input unexpectedly enables PTY process spawning")
    enabled_graphs: dict[str, set[str]] = {}
    # getrandom already occurs transitively in the host substrate. Prove this
    # capability activates the backend without importing unrelated facilities;
    # default absence of the package would be a false claim.
    entropy_graph = tree("secure-random")
    if "getrandom" not in entropy_graph:
        failures.append("secure-random graph omits its OS entropy backend")
    unexpected_entropy = sorted(
        (entropy_graph - default_graph)
        & (SKETCH_AND_WEBVIEW_PACKAGES | {"crash-handler", "console-subscriber", "mimalloc-pprof", "framehop"})
    )
    if unexpected_entropy:
        failures.append(f"secure-random adds unrelated facilities: {', '.join(unexpected_entropy)}")
    failures.extend(build_resources_consumer_failures())
    failures.extend(rustls_client_failures())
    for feature, package in CASES:
        if package in default_graph:
            failures.append(f"RED failed: default graph unexpectedly contains {package}")
        if feature not in enabled_graphs:
            enabled_graphs[feature] = tree(feature)
        enabled_graph = enabled_graphs[feature]
        if package not in enabled_graph:
            failures.append(f"GREEN failed: --features {feature} omits {package}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("dependency isolation RED -> GREEN checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
