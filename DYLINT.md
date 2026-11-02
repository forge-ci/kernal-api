# Client boundary lint

The suite contains five lints:

- `kernal_api_boundary` rejects direct client use of implementation crates.
- `kernal_api_unbounded_wait` rejects a client call to a kernal-api wait that
  has no time budget when a bounded replacement exists (#433), for example
  `Mutex::lock` instead of `Mutex::lock_within(timeout)` or
  `platform::fs::lock_exclusive` instead of `lock_exclusive_for(file,
  timeout)`. It matches the resolved definition, so alias imports, UFCS calls
  and function items are caught too. It also rejects `.await` applied directly to a
  kernal-api `Task`, `OneshotReceiver`, `Notified` or `OwnedNotified`, which
  have `join_within` / `recv_within` / `wait_within` forms. The full list is `UNBOUNDED_WAITS` in
  `dylints/kernal_api_boundary/src/lib.rs`, kept in step with
  [docs/wait-inventory.md](docs/wait-inventory.md). Service-lifetime listeners
  such as `accept` and interrupt waits are not on it. `kernal-api` itself is
  not checked yet: its own callers still migrate under #433.
- `kernal_api_guard_across_await` rejects a client `async` body that keeps a
  kernal-api `Mutex` or `RwLock` guard alive across an `.await` (#433). It
  reads the coroutine's saved state, so a guard inside an `Option`, a tuple,
  a collection or a local struct is caught too. A guard dropped or scoped out
  before the `.await` is fine. Semaphore permits carry no data and are not
  checked.
- `kernal_api_blocking_in_async` rejects a client call to a blocking
  kernal-api operation made directly in an `async` body (#433): a runtime
  bridge (`Runtime::run`, `run_within`), a blocking lock or send, a
  synchronous child wait, a file lock, or a synchronous IPC / broker connect.
  Bounded or not, it parks a runtime worker. A closure nested in the async
  body, such as the one passed to `launch_blocking`, is its own body and is
  not checked. The list is `BLOCKING_CALLS` in the same file.
- `kernal_api_platform_boundary` rejects host `cfg` selection and native OS
  APIs outside the private platform trees of `kernal-api`, in clients and in
  `kernal-api` itself, including code elided on the CI host.

Add the repository lints to a client workspace:

```toml
[workspace.metadata.dylint]
libraries = [
  { git = "https://github.com/zackees/kernal-api", pattern = "dylints/*" },
]
```

Then run it through the client's Soldr toolchain front door:

```console
soldr cargo dylint --all --workspace -- --all-targets
```

## What the repository's own lint job covers

A Dylint pass only sees code the compiler compiles, and `default = []` here.
The `dylints` job in `.github/workflows/ci.yml` therefore runs both lints as

```console
soldr cargo dylint --all --workspace -- --all-features --all-targets
```

on Linux and Intel macOS. `--all-features` puts every gated module in front of
the lint -- `crash`, `wasm`, `symbolize`, `profile`, `snapshot`, `fs`,
`fs-watch`, `ipc`, `pty`, `tokio-console`, `window-icon`, the daemon slices --
and `--all-targets` adds the integration tests and the two `required-features`
worker binaries.

The Linux matrix entry also materializes and cross-lints both supported Windows
targets plus Apple ARM with the same all-feature shape:

```console
soldr cargo dylint --all --workspace -- \
  --all-features --all-targets --target x86_64-pc-windows-msvc
soldr cargo dylint --all --workspace -- \
  --all-features --all-targets --target aarch64-pc-windows-msvc
soldr cargo dylint --all --workspace -- \
  --all-features --all-targets --target aarch64-apple-darwin
```

A green `dylints` job therefore resolves and checks Linux x86-64, macOS Intel
and ARM64, plus Windows x86-64 and ARM64 selected source, not just the ungated
core. The cross-target steps verify that each nightly target has real `libcore`
and `libstd` archives before linting, rather than trusting rustup's cached
target bookkeeping.

Linux ARM is not yet cross-linted: the all-feature graph needs an ARM GLib
sysroot that the current managed Linux target toolchain does not provide. Its
native ARM build lane still compiles the product graph, and the platform
boundary's source scan covers its selected files regardless.

`tests/facade_policy.rs` still scans all source as text regardless of host. It
joins a declaration wrapped across line breaks before matching it, so a backend
path on a continuation line is covered too. The scan remains deliberately
coarse: it matches spellings rather than resolved types, so an alias that
renames an owned crate requires the resolving lint coverage above. Narrowing
the feature set or target set is a coverage decision, not a knob: if either is
ever narrowed, say here exactly what remains covered.

## Platform boundary

`kernal_api_platform_boundary` enforces the layout in
[docs/platform-boundary.md](docs/platform-boundary.md) on every crate it
runs on, `kernal-api` included: the only host `cfg`, native OS APIs and
concrete-tree names allowed are those inside the private platform trees that
the one exactly shaped `std::cfg_select!` at the root of this library
declares, plus that crate's root-level `use platform_imp::...;` bridge. A
client cannot declare a selector of its own, because the selector is
accepted only in the `kernal-api` library root.

It is a pre-expansion lint that parses each module's source into token
trees and follows every out-of-line `mod` whatever `cfg` guards it, so the
Linux pass inspects Windows- and macOS-only files, unit and integration tests
included; comments and string literals cannot match. The same scanner runs
without compiling anything as the lint crate's repository test, over every
target of every manifest the lint crate classifies as enforced:

```console
soldr rustup run nightly-2026-08-15 cargo test \
  --manifest-path dylints/kernal_api_platform_boundary/Cargo.toml --lib
```

CI runs that test on every pull request and push; the full-mode `dylints`
job adds the UI tests and the resolving passes above. Existing debt is listed
exactly in `src/baseline.txt`, which may only shrink; see
[docs/platform-boundary.md](docs/platform-boundary.md). There are no waivers.

The lint checks both the client manifest and resolved Rust code. An unused,
aliased, target-specific, build, or test dependency on a facade-owned backend
is rejected before it can create a duplicate compile unit; method calls,
function items, imports, and public type references are also resolved to the
underlying crate and rejected. Client APIs must expose `kernal_api`-owned
facade types instead.

Inside `kernal-api` the same lint applies the complementary rule. A backend may
be used privately there, but it may not appear in a public type position: the
payload of a public enum variant, a public field, a function parameter or
return type, a type alias, a const or static, or a bound. A backend named in
one of those is vocabulary a client has to speak in order to match on or call
the item, whether or not it is re-exported, so a `pub use` grep does not see
it. A private field of a public newtype, a private item, and a trait
implementation that adapts a facade type into a backend one all remain legal.

An exported `pub use` of a backend item or module is rejected as well, including
a rename such as `pub use running_process::StreamKind as CaptureStream` and a
glob. Renaming changes only the spelling: the re-export still resolves to the
backend's definition, so a client receives the backend type and inherits its
versioning. Private and `pub(crate)` imports, and a `pub use` inside a module no
client can reach, stay legal. `tests/facade_policy.rs` repeats the textual half
of this check for hosts the Linux lint job never compiles.

The owned set is not only third-party backends. It also carries the raw host
bindings -- `libc`, `mach2`, `winapi`, `windows_sys` -- for the same reason:
#77 collapsed the per-host bindings into this crate's private HAL, so a client
that names one is speaking the host vocabulary the facade exists to absorb,
and a `libc::termios` or a `windows_sys` handle in a public position here is
the same leak as a backend type. Heavy private use inside the platform trees
stays legal, exactly as it does for any other owned crate.

The same coupling arrives from the other direction when this crate implements
a backend's trait for one of its own exported types. `impl
tokio::io::AsyncRead for IpcAsyncStream` names no backend in any signature,
yet a client cannot call through it without `use tokio::io::AsyncRead`, so the
backend is back in the application's import list. The rule splits that class
in two.

Implementing the async mirrors of `std::io::{Read, Write, Seek, BufRead}` --
`tokio::io::AsyncRead`, `AsyncWrite`, `AsyncSeek`, and `AsyncBufRead` -- is
allowed, and is what a usable stream facade is for. Those four are shared
byte-stream vocabulary rather than backend design: the impl says "this is a
byte stream", not "this is an interprocess socket", and the ecosystem's
combinators, codecs, and protocol crates are all written against them. A
facade-owned substitute would interoperate with none of them, so refusing the
impl would make the facade's stream types useless in the one position they
exist to fill. The facade still owes a caller an inherent method for the
ordinary operations, so that reaching a stream's `read` or `write_all` needs
no extension-trait import; the trait implementations are for handing the
stream to someone else's generic code.

Implementing any other owned-crate trait for an exported type is rejected.
Those are backend extension points -- `framehop::ModuleSectionInfo`,
`notify::Watcher` -- and implementing one for a type a client can name
publishes the backend's design as this crate's contract, which is the drift
the boundary exists to prevent. Two shapes stay outside the rule because they
publish nothing. A backend trait implemented for a private type is
unreachable, since no client can name the type it is attached to. A
facade-owned trait implemented for a backend type -- the adapter direction --
is reachable only by a caller that already holds the backend type, so it
imposes no vocabulary on one that does not.

`running-process` is classified as an owned implementation dependency, with
the same rule as Tokio and no approved exceptions. It is allowed privately
inside `kernal-api`, where the private adapter now lives
(`src/process_adapter.rs`, on a mandatory dependency) alongside the
facade-owned `independent-spawn` capability. It is denied in each first-party
application once that application's required process and broker facade is
available.
`running-process` must never depend in the opposite direction.

Adoption is a capability-by-capability ratchet, not a flag-day waiver. Land a
facade with behavior and compatibility tests, migrate the client call sites,
then enable the strict ban for that client workspace. Temporary migration
branches may carry both dependencies, but released code has no alternate
runtime, network stack, process layer, or broker path behind a fallback.

This document previously said that `blake3` was not yet in the owned
implementation set, on the reasoning that the `kernal_api::hash` facade landed
in #8 before any first-party client had migrated its content-hashing call
sites. That is out of date and has been for some time: `blake3` is listed in
`OWNED_IMPLEMENTATION_CRATES` (`dylints/kernal_api_boundary/src/lib.rs`), and
the ban applies unconditionally -- `owned_crate_name` and
`direct_owned_dependencies` consult that list with no per-client gate. A client
that adds a direct `blake3` dependency is rejected today rather than pending a
future migration.
