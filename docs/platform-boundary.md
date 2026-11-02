# Native platform boundary

This is the authoring guide for native-host platform code in `kernal-api`.
It is the canonical description of the boundary, and the
`kernal_api_platform_boundary` Dylint enforces it on this crate as well as on
its clients ([#152](https://github.com/zackees/kernal-api/issues/152)).

## One structured selector

`src/lib.rs` owns the only native-host selector. Its `std::cfg_select!` block
maps Windows, Linux, and macOS to private concrete trees and gives the selected
tree the private `platform_imp` name. The crate root then exports semantic,
platform-neutral operations. Capability modules consume those operations; they
do not select an OS themselves.

```rust
// src/lib.rs: the one native-host selection point
std::cfg_select! {
    target_os = "windows" => { mod platform_win; pub(crate) use platform_win as platform_imp; }
    target_os = "linux" => { mod platform_linux; pub(crate) use platform_linux as platform_imp; }
    target_os = "macos" => { mod platform_macos; pub(crate) use platform_macos as platform_imp; }
}
```

Concrete OS implementation belongs under the selected platform trees. It may
use native APIs there, but it must export a semantic operation that works for
every supported host. If a capability is unsupported, model that explicitly
in the facade contract instead of spreading OS checks through callers.

## Authoring rules

- In ordinary `src/` modules, use the crate-root facade. Do not add
  `#[cfg(target_os = ...)]`, `cfg!(target_os = ...)`, `std::os::*`, native OS
  crates, `platform_imp`, or a concrete platform module.
- Host-neutral feature gates and test configuration remain normal Rust tools.
  They are not an exception for choosing a native implementation.
- WebAssembly/guest target selection is not native-host selection. Keep those
  branches conceptually and structurally separate.
- Add a native capability by defining its semantic facade operation,
  implementing it in each concrete platform tree, then exposing it once from
  the crate root. Preserve Linux, macOS, and Windows on x86-64 and ARM64.
- **Duplication is preferred to a selector in the neutral facade.** The rule is
  about where a `cfg` selector may appear, not about which selector it is:
  `target_arch`, `target_env`, `unix` and `windows` are banned outside the
  concrete trees exactly as `target_os` is, and
  `dylints/kernal_api_platform_boundary/src/lib.rs` lists all ten. When a
  capability needs one, copy the implementation into all three trees even if
  the copies are identical and the selection has nothing to do with the OS.
  Three copies are cheaper to own than one exception, and an exception is what
  a future reader will cite for theirs.

For example, a capability module should call a neutral operation:

```rust
let shell = crate::shell_spec(command);
```

It should not select a concrete operating system:

```rust,ignore
#[cfg(target_os = "linux")]
use std::os::unix::process::CommandExt;
```

An arch-only selector in a neutral leaf is the same violation, however
host-independent the code it guards. `platform::host::cpu_compatibility_features`
is the worked example: the probe is pure `std::arch`, identical on every host,
and it still lives in `platform_linux/host.rs`, `platform_macos/host.rs` and
`platform_win/host.rs`, with `src/platform/host.rs` re-exporting the bridged
`host_cpu_compatibility_features` and carrying no `cfg` of its own.

## Where host code may live

| location | host `cfg` / native API / concrete-tree names |
|---|---|
| `src/lib.rs`: the one host `cfg_select!` | exactly three arms, `target_os = "windows"`, `"linux"`, `"macos"`, each `mod platform_x; pub(crate) use platform_x as platform_imp;` (a `#[path]` on the module is allowed); no `_` fallback, no `unix`, no second selector |
| `src/lib.rs`: root-level `use platform_imp::...;` | the crate-root bridge, and the only place outside the trees that names `platform_imp`; not inside a function or an inline module |
| `src/platform_win{.rs,/**}`, `src/platform_linux{.rs,/**}`, `src/platform_macos{.rs,/**}` | anything: native APIs, arch selection, native tests |
| `src/guest.rs` and its modules (the root `target_family = "wasm"` arm) | guest predicates only: `target_family = "wasm"`, `target_arch = "wasm32"`, `target_os = "wasi"` / `"unknown"` |
| everything else: the rest of `src/`, `src/bin/`, `tests/`, examples, benches, `build.rs`, client crates | none |

The concrete trees are whatever the host selector declares, so no path is
exempt by name: move a file out of a declared tree and it is neutral source
again. The host selector is accepted only in the `kernal-api` library root
(`src/lib.rs` relative to its manifest), so a client or test crate cannot
declare trees of its own.

"None" covers every spelling the scanner can see before expansion:

- `#[cfg]`, `#![cfg]`, `#[cfg_attr]`, `cfg!` and `cfg_select!` whose predicate
  names any of `windows`, `unix`, `target_os`, `target_family`, `target_arch`,
  `target_abi`, `target_env`, `target_vendor`, `target_endian` or
  `target_pointer_width`, nested under `all`/`any`/`not` or mixed with
  feature and test predicates.
- paths rooted at a native binding crate -- `libc`, `mach2`, `winapi`,
  `windows`, `windows_sys`, `windows_core`, `x11rb`, `gtk`, `gdk`, `glib`,
  `webkit2gtk`, `objc2`/`objc2_*`, `block2`, `core_foundation`,
  `core_graphics`, `nix`, `rustix`, `dispatch2` -- or at `std::os`/`core::os`,
  `tokio::{net,signal,process}::{unix,windows,Unix*}` and `interprocess::os`,
  foreign import blocks (`extern "C" { .. }`) and `#[link]`,
  including renamed imports (`use libc as c`), `extern crate`, absolute
  `::libc` paths, braced `use std::{os::unix, ..}` trees and macro bodies.
- `platform_imp`, `platform_win`, `platform_linux` and `platform_macos` as
  identifiers.

Feature, test, `docsrs`, `debug_assertions` and other host-neutral
predicates stay legal everywhere. Comments and string literals never match.
Test code is not an exemption: a test that asserts native mechanics lives in
the concrete tree beside the code it tests, as a `#[cfg(test)]` module in
each tree it applies to, and a generic test in `tests/` asserts through the
public facade on every host.

## Tests and expectations that differ per host

An integration test in `tests/` cannot `cfg` its expected value. Assert
against what the facade itself reports for this host -- the executable
suffix, the shell, the default permission mode -- or move the native
assertion into the tree that owns the mechanism. A test that needs a native
observation (a pidfd, a job object, a kqueue) gets it from a narrowly scoped,
feature-gated test-support operation implemented in each tree, never from
`libc` or `windows_sys` in the test file.

## Validation

The lint parses each crate's modules from source and follows every
out-of-line `mod`, whatever `cfg` guards it, so a Linux pass inspects the
Windows and macOS trees' neighbours too. Run it through the Soldr front door:

```console
soldr cargo dylint --all --workspace -- --all-features --all-targets
```

The same scanner runs without compiling the product as the lint crate's
repository test, which walks every Cargo target of every enforced manifest
(from `cargo metadata`) and fails on any violation:

```console
soldr rustup run nightly-2026-08-15 cargo test \
  --manifest-path dylints/kernal_api_platform_boundary/Cargo.toml --lib
```

CI runs that test in every mode, and the full-mode `dylints` job adds the
UI tests and the resolving Dylint passes for Linux, macOS and both Windows
targets.

Until the migration finishes, the existing debt is recorded in the exact
occurrence baseline `dylints/kernal_api_platform_boundary/src/baseline.txt`
(the format zccache used for its own migration). Each TAB-separated row is a
repository-relative path, a kind (`host_cfg`, `native_api`, `concrete_tree`,
`selector`, `unreadable`), the normalized construct, and its ordinal among
identical constructs in that file; a `# total = N` header counts the rows.
Line numbers are deliberately absent. The repository scan requires an exact
match: a new occurrence fails (a second copy in a listed file takes the next
ordinal; a moved file changes its path), and a stale, duplicate or unsorted
entry fails too, so fixing a violation means deleting its line and
decrementing the total. Never add a line. The resolving Dylint pass skips
only baselined occurrences. Migrating to zero deletes the file and
`src/baseline.rs`, as in Soldr and zccache.

Every `Cargo.toml` in the repository is classified in
`dylints/kernal_api_platform_boundary/src/lib.rs` as native (the workspace,
the `tests/*-consumer` client fixtures, `tools/wasm-abi-generator`,
`benchmarks/wasm-sketch/component-tools`), guest (the wasm guest crates and
the generated guest ABI bindings, scanned under the guest rule so that guest
predicates and wasm imports pass but native host selection does not, even
when a native tool `include!`s them) or lint (the two lint crates, whose UI
fixtures violate on purpose). Adding a manifest without classifying it fails
`every_manifest_is_classified`.
