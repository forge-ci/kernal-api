# Architecture

This document is the canonical dependency and ownership design for
`kernal-api`. Earlier proposals that placed `running-process` above the facade
or required a particular crate-packaging scheme are superseded by this design.

## Dependency direction

```text
zccache / soldr / fbuild
          |
          v
      kernal-api
          |
          v
   running-process
          |
          v
 Tokio and native operating-system APIs
```

`running-process` is the trusted native substrate. It already owns substantial
cross-platform process, lifecycle, and broker implementation code, and it must
have an async runtime to do that work. It must never depend on `kernal-api`.

`kernal-api` is the higher semantic facade used by applications. It depends on
`running-process`, selects shared implementation versions, adds facilities such
as hashing and profiling, and turns backend behavior into stable application
contracts. The phase-1 bounded process adapter has landed: it uses the exact
published `running-process` 4.10.20 release with its lightweight
`kernel-substrate` feature, as a mandatory dependency. The facade-owned
`independent-spawn` capability is an explicit feature. First-party
applications eventually depend on `kernal-api` only.

This one-way graph resolves the async/process cycle without creating a smaller
"base" facade that would merely move the same boundary elsewhere.

## Boundary rules

- `running-process` owns low-level process, OS, and current broker mechanisms.
- `kernal-api` owns public semantic types, policies, defaults, and capability
  composition. It adapts `running-process` privately and never publicly
  re-exports, aliases, or names its types: the same rule as Tokio and every
  other implementation crate, with no exceptions. The `independent-spawn`
  capability (`SpawnMode`, its options/backend/lifetime, launch payload,
  readiness, handle/exit, and `spawn_with_options`) is facade-owned and
  converts to the substrate privately; its live handle keeps the substrate
  handle in a private field.
- Applications own product policy and product protocols. zccache, for example,
  keeps its cache payload schema, protocol identifiers, and deployment policy.
- Applications may not directly depend on `running-process`, Tokio, or another
  implementation for a capability that `kernal-api` provides.
- New facade operations bake in safe defaults: bounded resources, cancellation,
  connection and progress timeouts, child cleanup, and diagnostic visibility.
- Process mutation is addressed by proof of ownership, never by a bare PID.
  `platform::process::force_kill`, `kill_tree`, and `set_priority` take a
  generation-checked `ProcessIdentity`; `force_terminate_process_group`
  borrows the owner's unreaped `std::process::Child`; and
  `executable_path` reads through a retained `ProcessLiveness`. Only
  read-only telemetry (`cpu_ticks_for_pid`, `peak_rss_bytes_for_pid`,
  `tree_rss_bytes_for_pid`) is PID-addressed, for a child the caller has not
  reaped.

The public API describes intent rather than backend vocabulary. This allows a
backend to be trimmed, vendored, or rewritten without changing every client.

### Native platform boundary

Host selection has one owner: the `std::cfg_select!` block in `src/lib.rs`.
It selects a private concrete platform tree and re-exports a neutral facade
from the crate root. Capability modules consume that facade; they do not make
their own `target_os` choices or name a concrete tree. Guest/WASM target
selection is intentionally separate from native-host selection. The detailed
authoring and validation guide is [docs/platform-boundary.md](docs/platform-boundary.md),
and the `kernal_api_platform_boundary` Dylint enforces it on this crate, its
tests and its clients alike (#152).

Filesystem mechanics that cache materialization depends on -- native
replacement with its Windows sharing-violation retry, link and reparse
classification, permission bits, path identity, and volume facts -- follow
the same shape: `src/platform/fs/{materialize,path_file,replacement}.rs` are
the neutral facade, and each concrete tree's `fs_materialize.rs` owns the
host half, reached through the crate-private `native_fs_materialize` name.

## WebAssembly sketch runtime

`kernal-api` will host long-lived Rust sketches compiled to WebAssembly. A
sketch is ordinary Rust business logic, including third-party `std` crates; it
is not restricted to a single-threaded or async-only programming model. Host
effects remain capability-controlled even when a dependency reaches them
through a standard-library compatibility adapter rather than calling the
`kernal-api` Rust crate directly.

```text
Rust sketch and third-party crates
              |
              | kernal-api guest facade or restricted std adapter
              v
 versioned resources and bounded streams
              |
              v
   kernal-api native sketch host
              |
              | private implementation
              v
      Wasmtime + Cranelift
              |
              v
    x86-64 / ARM64 machine code
```

Wasmtime is the selected private execution engine and Cranelift is its selected
private code generator. The sketch API does not expose either implementation,
and there is no second engine fallback. Rust-to-WASM compilation and
WASM-to-native compilation are separate cached stages. Any serialized native
code cache is keyed by engine version, configuration, host target, and CPU
features; the input WASM remains the portable distribution artifact.

The sketch host is heavyweight and feature-gated. `default = []` must not
resolve or compile Wasmtime, a binding generator, or the sketch-host runtime.
The binding implementation is also private and must satisfy this contract
whether it is generated by an owned fp-bindgen fork or another mechanism.

### Threaded Rust contract

Guest threads are a v1 requirement. Sketches may use `std::thread`, `Arc`,
atomics, mutexes, condition variables, channels, Rayon-style worker pools, and
concurrent collections such as `DashMap`.

The implementation uses one precompiled module and instance-per-thread
execution. Each guest thread owns a Store, Instance, stack, TLS, and execution
state. All threads in one logical sketch share its linear memory and heap,
compiled code, allocator state, kernel runtime, capability set, and resource
registry. Cloning per-Store host state clones facade-owned references to those
shared services; it never constructs another Tokio or kernel runtime.

Thread creation is a kernel-owned bootstrap capability. A standard
`thread-spawn` import may be adapted or renamed after linking so ordinary
third-party `std::thread` code works without gaining ambient WASI authority.
Final-module validation permits only versioned kernel imports and explicitly
owned compatibility imports. Filesystem, network, process, clock, random,
environment, and other authority is granted per sketch rather than inherited
from the host process.

The host enforces a maximum guest-thread count and an explicit shared-memory
maximum. Shared memory is accounted by `kernal-api` rather than relying solely
on an engine Store limiter, and the threaded profile does not depend on the
pooling allocator. Issue #43's in-process epoch clock is cooperative: it
cannot terminate an `atomic.wait` or host-blocked call. Until all blocked
atomic waits can be interrupted reliably,
untrusted threaded sketches run behind a killable worker-process boundary so a
timeout, trap, or teardown can contain every thread and release every resource.

### Async and blocking guest surfaces

Async is a host implementation property and an available guest API style, not
a restriction on application structure. Kernel I/O remains asynchronous and
may suspend one guest Store while other guest threads and native work continue.
The guest facade also provides blocking adapters for threaded code and standard
traits such as `Read`, `Write`, and `Seek`. Both surfaces use the same resource
and streaming protocol; a blocking adapter may park its guest thread but may
not turn a stream into a whole-value transfer.

Ordinary bounded manipulation of guest-owned memory remains synchronous.
Application code must not retain a `DashMap`, mutex, resource-buffer, or other
critical guard across an async call, blocking host operation, or mutation that
can require the same guard. This rule applies even when multiple guest threads
could eventually make progress, because cancellation and teardown must not
strand kernel resources behind application locks.

### Backpressure ownership

`kernal-api` owns backpressure at every host/guest resource boundary. Cranelift
only generates native instructions and Wasmtime only suspends and resumes guest
execution; neither defines buffering policy. The public semantic contract and
the native capability implementation together enforce:

- bounded chunk sizes, bounded queue depth, and a per-resource and per-sketch
  maximum for in-flight bytes;
- pull-based reads that produce no additional data until the consumer requests
  another bounded chunk;
- capacity-awaited writes that stop accepting data until downstream capacity
  is released;
- ordering and sharing rules for concurrent callers, with deterministic errors
  when a resource is not shareable;
- EOF, partial writes, half-close, cancellation, progress timeouts, and explicit
  overflow behavior;
- generation-safe handles, cross-sketch rejection, and permit/buffer cleanup on
  close, cancellation, trap, thread exit, or sketch teardown.

Blocking `Read` and `Write` adapters use the caller's buffer length as the
transfer bound. Async operations remain pending when capacity is exhausted.
Neither interface may allocate or serialize an entire file, artifact, archive,
process output, directory walk, or network body merely to cross the ABI.

Backpressure is specialized where the native source cannot itself pause:

- file and network reads are demand-driven;
- process stdout and stderr remain bounded by the host and operating-system
  pipes rather than being drained into an unbounded host queue;
- directory walks and archive inventories use cursors and bounded pages;
- file watchers use bounded event queues and report an explicit
  `overflow/rescan-required` condition when the operating system cannot pause
  event production;
- hashing, copying, sealed-blob verification, and similar bulk pipelines remain
  host-side when the sketch needs only their result.

This ownership rule is part of the facade contract, not a property delegated to
an engine, binding generator, Tokio channel default, or application-specific
buffering convention.

## Migration sequence

1. Fill the facade gaps needed by zccache: cancellation/runtime handles,
   process lifecycle and bounded execution, BLAKE3 hashing, and broker adapters.
   The async compiler-session path is covered by `SpawnSpec::spawn_session`
   with `SpawnAdmission` and `SpawnSpec::priority_best_effort`, adapted
   privately in `src/process_adapter.rs`.
2. Rebase zccache's embedded API on facade-owned async and cancellation types,
   including the std-only `fair_race!`, `biased_race!`, and `task_local!`
   macros that replace backend `select!` and task-local call sites.
3. Move zccache process launch, probing, detached deployment, identity, and
   hashing behind `kernal-api`.
4. Move broker access through a compatibility facade while preserving the
   existing frame bytes, version negotiation, endpoint behavior, and number of
   process-to-process round trips.
5. Remove every direct zccache `running-process` dependency and import, then
   enable the strict boundary Dylint for the whole workspace.
6. Apply the proven migration to Soldr and fbuild.

Direct-daemon identity (step 3) is owned by the opt-in `daemon_identity`
facade: the frozen sidecar, probe, and endpoint mux delegate privately to the
substrate's `backend-identity` feature, while recorded-daemon verification and
control run on `kernal-api`'s own host process facade so the feature never
selects the substrate's broker client.

During migration, each capability lands in `kernal-api` before the corresponding
client ban is enabled. There is no permanent legacy fallback in release builds.

## Broker direction

The broker daemon implementation remains in `running-process` during the first
migrations. This avoids combining an architectural cleanup with a wire or
lifecycle rewrite. Clients reach it through the opt-in `broker_client` adapter
(feature `broker-client`), which owns the request, connection, route, refusal,
and error values and converts the substrate's client types privately.

After zccache, Soldr, and fbuild consume the facade successfully, the generic
broker-daemon pattern should move up into `kernal-api` as a managed-service
capability. That later project must preserve compatibility and keep application
payloads outside the generic layer. It is explicitly not a phase-1 prerequisite.

## Source and package organization

One supported public API does not require one Rust compilation unit. Public
surface stability and physical crate boundaries are separate decisions.

Start with cohesive, feature-gated modules. Split private implementation crates
only when reproducible `soldr cargo` timings show a material win in clean build,
incremental rebuild, cache reuse, or feature isolation. A release amalgamation
is also an optimization to prove, not a permanent architectural requirement.
Optional features must omit their dependency trees when disabled. `window-icon`
is the one recorded exception, and a surface-only one: `running-process`
declares `png` and `x11rb` non-optionally on Linux, so gating this crate's own
GUI capability cannot remove them from the graph. It is therefore proved by
`tests/facade_policy.rs::window_icon_stays_an_opt_in_gui_capability` rather
than by a case in `ci/check_compilation_boundary_dependencies.py`, which can
only assert absence from the graph. The exception ends when the substrate
gates its own copy.

### Current compilation-boundary decision (issue #3)

The selected organization remains one cohesive `kernal-api` package with
feature-gated modules. The current record in
`benchmarks/compilation-boundaries/results/` records the current package and a
real pre-adapter historical baseline (`f745dcff4e2874170b779c510c698258aee90055`)
using the checked-in protocol. The baseline quantifies the substrate migration;
it is not a packaging A/B comparison because the intervening revisions also
evolved facade and platform code. It records clean and representative
incremental critical-path time, peak RSS where the host provides it, Soldr
cache snapshots, debug output size, and packaged archive size.

Build-script helpers follow the same rule: Windows executable resources are
the `build-resources` feature (`kernal_api::build_resources`), taken as a
second, same-name build-dependency, not a separately published
`kernal-api-build` package. That package was never published and is gone.

No change has demonstrated a cohesive implementation-only unit whose split
would pay for another compilation boundary. Therefore no private crate and no
release amalgamation is adopted. A later split proposal must compare its shape
with the same facade and feature set under this protocol; a private crate must
be `publish = false` and remain behind facade-owned types. An amalgamation
additionally requires deterministic archives and archive-extracted verification
before it can replace ordinary Cargo packaging. Dependency isolation is
continuously proved with feature RED -> GREEN checks in
`ci/check_compilation_boundary_dependencies.py`.

Do not create speculative abstraction crates or traits such as a generic
`kernal-api-data` layer without multiple real implementations. Initially,
hashing, mapped files, file locks, databases, networking, HTTP services, and GUI
hosting belong in capability modules with facade-owned concrete contracts.
