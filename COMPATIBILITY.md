# Compatibility and version policy

`kernal-api` owns the systems dependency versions that must coexist in one
process. Its direct dependency requirements are exact and release tests keep
them aligned with the checked-in lockfile. Clients do not select parallel
implementations of these facilities.

| Contract | Supported baseline |
|---|---|
| Rust | exactly the project's 1.95.0 MSRV/toolchain floor or newer |
| Python | 3.10+ |
| Operating systems | Linux, macOS, Windows |
| Architectures | x86-64, ARM64 |
| Async engine | `kernal_api::async_engine`, backed by exactly Tokio 1.53.1 |
| Heap allocator/profiler | exactly `mimalloc-pprof` 1.1.0 (`pprof` feature) |
| Native crash interception | exactly `crash-handler` 0.7.0 |
| Async task protocol | exactly `console-api` 0.9.0 / `console-subscriber` 0.5.0 |
| CPU/async export | one checked-in `perftools.profiles` schema |

## Wasm fuel and epoch characterization (#44)

The feature-gated `wasm-sketch-host` resolves **Wasmtime 45.0.0** from the
checked-in Cargo lockfile. The facade keeps its backend types private while
characterizing these semantic boundaries:

- a real Wasmtime `OutOfFuel` trap maps to `SketchExecutionError::OutOfFuel`
  for both root and ordered child execution, even if an epoch terminal winner
  was also observed;
- epoch deadline observation for ordinary compute is finite under a configured
  tick/deadline but wall-clock delivery is scheduling-dependent; same-tick
  cancellation deterministically wins deadline selection;
- root failure takes precedence over ordered child failure, followed by private
  validation reporting; cooperative terminal paths release epoch, Store,
  instance, and root accounting;
- an `atomic.wait` or host-blocked call is not an in-process cancellation
  promise. It is classified as `ContainmentRequired`, characterized only in a
  killable subprocess, and worker-process reaping remains issue #28.

The default feature set remains empty: Wasmtime and this characterization are
not selected by ordinary async/process users.

| Target | Native evidence for this boundary |
|---|---|
| Linux x86-64 | Managed Bosn 0.1.3 (`uname -m` = `x86_64`): current-tip fmt, warnings-denied all-feature lint, and locked all-feature suite passed (jobs `j1-2957e646`, `j2-20e8e2d9`, `j3-c11d96c9`); both exact containment subprocess tests also passed (`j6-c8de8973`, `j7-943bab55`). |
| Windows x86-64 | Native Soldr current-tip CI-equivalent all-feature suite passed: 369 library tests, all integration/doc tests, with only the two established PDB line-resolution quarantines skipped. |
| macOS x86-64 / ARM64 | pending/not run |
| Linux ARM64 | pending/not run |
| Windows ARM64 | pending/not run |

This is an evidence matrix, not a statement of test execution. Supported
targets remain those in the package policy above; unlisted native runs must be
recorded before being claimed as evidence.

## Wasm worker containment (#28)

`wasm-sketch-worker` places one request in a private contained process
boundary. The facade reports typed worker diagnostics and maintains its parent
lease/gauge accounting through normal completion, cooperative cancellation,
deadline handling, unexpected exit, and bounded forced cleanup; it does not
expose child handles, Job Objects, pidfds, or backend worker types. Windows
uses kill-on-close Job containment and Linux uses parent-death signaling, so a
supervisor's abrupt death also contains the worker. The optional
`wasm-sketch-worker-test-support` hook is test-only and not a public API: its
external proof marker records a PID together with an opaque native creation
key, never a PID alone. The broader universal identity API remains follow-up
#51.

`default = []` remains isolated from the Wasm host, worker, and test-support
feature. The process boundary is therefore opt-in rather than a new default
process/runtime dependency.

| Target | Focused #28 evidence |
|---|---|
| Windows x86-64 | Native local Soldr passthrough with `ZCCACHE_DISABLE=1`: the four core D4 containment tests plus crash and parent-death proofs passed (6 passed); the two exact, ignored inner helpers were intentionally not part of the normal run. Prior current-tip checks, lint, and regression evidence also passed. |
| Linux x86-64 | Managed Bosn 0.1.3 native evidence: `doctor` healthy (+0.130 s), `CARGO_BUILD_JOBS=1`; fmt, test-support check/clippy, parser/identity, and containment passed (6 outer; 2 intended ignored inner helpers, including pidfd crash and `PR_SET_PDEATHSIG` parent-death). All-feature check/clippy/full passed: 323 library + 54 symbolize + 14 worker tests, integrations/docs, and containment 6/2. No-default check/test passed: 112 library tests plus integrations; its tree excludes Wasmtime, wasmparser, and wat. Persisted jobs `j15-cbae57c3`–`j20-805af44d`; cleanup `j22-ae169c23` left zero sessions/artifacts. |
| Windows ARM64 | Compile-only; native focused evidence pending. |
| macOS x86-64 / ARM64 | Compile-only; native focused evidence pending. |
| Linux ARM64 | Pending. |

This matrix records platform evidence rather than expanding the supported API
surface. Native crash/parent-death claims require exact PID-plus-creation-key
observation and bounded disappearance/exit evidence on the listed target.

Linux x86-64 was revalidated locally at `d6e9882` with
`soldr cargo test --locked --features wasm-sketch-worker,wasm-sketch-worker-test-support --test wasm_worker_containment -- --test-threads=1`:
six outer tests passed in 12.06s, including exact-identity crash/parent-death
checks and sequential teardown stress. Five tests were intentionally ignored:
two externally controlled inner helpers and three artifact-dependent tests.
The latter three then passed separately in 30.39s using the existing admitted
threaded-smoke artifact and the filter `cargo_built_threaded_guest -- --ignored
--test-threads=1` with the same Soldr features/test target. They cover contained
execution, parent-owned output commit, and forced-output staging cleanup.
This does not establish parent-death cleanup of output staging, native GUI
descendant teardown, or fresh evidence on the other five targets.

## Client rule

Until 1.0, the four first-party clients use an exact Cargo requirement:

```toml
kernal-api = { version = "=0.1.36", features = ["..."] }

[profile.dev.package.kernal-api]
codegen-units = 1

[profile.test.package.kernal-api]
codegen-units = 1
```

The Python companion is likewise pinned with `kernal-api==0.1.34` when used by
first-party Python tooling. A source checkout may temporarily use a path patch
only on an explicit migration branch; release branches must resolve the exact
registry version. There is no `optional = true` legacy implementation behind
the same client operation and no runtime fallback to a second HAL.

## Ownership boundary

- `platform`: process, filesystem, IPC, PTY, terminal, host identity, and
  resource operations.
- `platform::fs` cache-materialization mechanics (feature `fs`): the
  `replacement` module (native rename replacement with the Windows
  antivirus/sharing-violation retry ladder, generation rename, delete-fallback
  replacement, staged directory install, and the Windows share/lock error
  classifiers), the `path_file` identity module, `LinkKind`/`classify`,
  `hard_link_count`, `symlink_file`, `set_readonly`, `make_executable`,
  `metadata_mode`/`apply_metadata_mode`, `file_change_marker`,
  `volume_identity_u128`, `file_id_width`, `allocated_bytes`,
  `native_call_path`, `path_from_raw_bytes`, and
  `sync_directory_if_supported`. Cache layout, retry budgets beyond the fixed
  ladder, and materialization tiers remain client policy.
  `DirectoryWalk::parallelism` selects `DirectoryWalkParallelism::SharedPool`
  (the default, which abandons a walk the saturated shared pool cannot serve
  within about a second) or `DedicatedPool { threads }`, a per-walk pool that
  never ends a walk because the machine is busy.
- `platform::ipc` owner-only single-instance transport (feature `ipc`):
  `LocalSocketListener::bind_owner_only` (pathname socket, mode `0o600`,
  observed `SocketPeerCredentials`), `LocalSocketStream`,
  `OwnerOnlyPipeInstance::create` (protected owner+SYSTEM DACL, remote
  clients rejected, optional first-instance exclusivity), `LocalPipeClient`
  (one open attempt, pipe-busy error preserved), and `retire_socket_endpoint`
  (unlinks sockets only; a no-op for named pipes). Byte I/O is polled with
  standard-library types; every host exports every name, and the pair that is
  not the host's transport is uninhabited and reports `Unsupported`. Endpoint
  spelling, retirement timing, pooling, retry, deadlines, and peer admission
  stay with the caller.
- Async process sessions (`SpawnSpec`, `SpawnAdmission`, `ProcessSession`,
  `ProcessSessionOptions`, `ProcessOutputEvent`, `ProcessSessionExit`): the
  one async spawn/stream/reap surface. It covers argument lists, spawn-time
  admission, best-effort scheduling bands, concurrent output and lifecycle
  control from shared `&self`, and lossless `ExitStatus` recovery.
  `ProcessOutputFault::new` builds a stream fault from its portable parts so
  consumers can test their completion handling. Failures
  surface as `std::io::Error` with the stable kind mapping documented in
  `src/process_adapter.rs`; the substrate's builder, session, and error types
  never cross it. Descendant-tree cleanup on drop is deliberately not offered.
- Host process control and telemetry in `platform::process`, on the default
  feature set: `configure_session_leader_command`, the owned-child
  `force_terminate_process_group(&std::process::Child)`, generation-checked
  `set_priority(ProcessIdentity, ProcessPriority)`, `executable_path(&ProcessLiveness)`,
  daemon bootstrap `detach_standard_streams` /
  `redirect_standard_streams_to_log`, the GNU make `NativeJobserver` with
  `native_jobserver_supported`, and the PID-addressed read-only readers
  `cpu_ticks_for_pid`, `peak_rss_bytes_for_pid`, `tree_rss_bytes_for_pid`
  (`MAX_TREE_RSS_PROCESSES`, `PEAK_RSS_READABLE_AFTER_EXIT`).
  `ProcessLiveness::has_exited` is the fallible exit question: `Err` reports a
  failed observation instead of reading it as an exit. `spawn_sync` and
  `spawn_sync_daemon` take a `SyncEnvironment` base -- `Inherit`, `Explicit`,
  or `UserBaseline`, the logged-in user's login environment rather than the
  launcher's ambient one -- with command-level variables applied last.
  `platform::executable::file_name_os` spells an executable name without
  losing native string data or doubling an existing extension. No PID-only
  forced termination is offered: terminate a bare PID through
  `capture_identity` and `force_kill`.
- `snapshot`: cooperative sibling-thread capture and deferred unwind.
- `crash`: the single native crash handler and bounded pre-crash spool.
- `profile`: bounded sampling, CPU/off-CPU aggregation, and pprof/Firefox/
  collapsed export.
- `symbolize`: protobuf-tagged ASLR-independent capture schema and isolated
  PDB/DWARF/Mach-O parser worker.
- `allocator`: the facade-owned allocator plus sampled heap lifecycle/dumps,
  the owned counter snapshot (`stats() -> ProfilerStats`, `HeapStats`), and
  the legacy text dump (`dump_file`). Clients must not name `mimalloc_pprof`
  or re-export it for an embedding host.
- `async_engine`: the facade-owned runtime/task surface and task diagnostics.
  It also owns cancellation tokens, connection deadlines, and progress/idle
  timeout policy; clients must not substitute a raw runtime or global transfer
  timeout for these contracts.
  It owns `select!`-shaped selection and task-local bindings too:
  `fair_race!` (two to five guarded branches, `FairRace2`..`FairRace5`),
  `biased_race!` (two to four branches in source order, `BiasedRace2`..
  `BiasedRace4`), and `task_local!` (`TaskLocal`, `TaskLocalScope`,
  `TaskLocalAccessError`). All three are std-only and expand to `$crate`
  paths; clients must not reach for `tokio::select!` or `tokio::task_local!`.
  It owns the fair write-preferring `RwLock` with borrowed, owned and blocking
  guard newtypes (`RwLockReadGuard`, `RwLockWriteGuard`,
  `OwnedRwLockReadGuard`, `OwnedRwLockWriteGuard`), the FIFO `Mutex` with
  `MutexGuard`, `OwnedMutexGuard` (which keeps its `Arc<Mutex<T>>` alive, so
  `Weak`-keyed lock registries see a held lock) and `MutexTryLockError`,
  `Notify::notified`/`owned_notified` futures (`Notified`, `OwnedNotified`)
  that can be enabled before waiting, `Task::detach_on_drop`,
  `RuntimeBuilder::max_blocking_threads`, `PeriodicTimer`'s
  `MissedTickBehavior` (`Burst`, `Delay`, `Skip`), and the ambient shutdown
  subscriptions `TerminationSignal` (SIGTERM; Windows console break, close
  and shutdown) and `wait_for_interrupt` (Ctrl+C) for launched work.
- `platform::window_icon` (feature `window-icon`): window and stock icon
  mechanics for the host console or a child, including the ICO/PNG decode and
  the X11 property write. GUI hosting is opt-in, so a headless client does not
  compile it. This gates the code and public surface, not yet the dependency
  graph: `running-process` declares `png` and `x11rb` non-optionally on Linux,
  so they still resolve in a default build until the substrate gates its own
  copy. `platform::window_icon`, `set_window_icon_impl` and
  `window_icon_support_impl` were available on the default feature set before
  this release; enabling `window-icon` is required as of it.
- `build_resources` (feature `build-resources`, outside `full`): build-script
  embedding of a Windows executable's icon, version information and
  Common-Controls v6 manifest. Applications add `kernal-api` a second time as
  a build-dependency with only this feature and call it from their own
  `build.rs`; the resource compiler and its backend stay private, and
  non-Windows targets are a no-op. This replaces the never-published
  `kernal-api-build` companion package: kernal-api is one package. A
  `kernal-api/<feature>` entry in the application's own feature table also
  reaches the build-dependency (Cargo applies it by name and rejects renaming
  one package twice), so forwarded capabilities compile for the build script
  too; see the module documentation for the library-crate workaround.
  It is host-only: it runs in a build script on the build host, so a graph
  linked for another target never includes it. CI's per-target matrix builds
  every feature but this one (`ci/target_features.py`); a Linux-hosted link
  for `*-pc-windows-msvc` cannot resolve embed-resource's `vswhom-sys`
  symbols, whose C++ archive is only built on a Windows host.
- `daemon_identity`, `daemon_frame_v1`, `daemon_registration`,
  `daemon_registration_v2` (features of the same names, each opt-in and
  outside `full`): the frozen v1/v2 daemon wires -- identity/sidecar/probe/mux
  semantics, the frame envelope codec, and the two registration record sets
  with their owner-private persistence. Endpoint naming, payload protocols,
  broker negotiation, and daemon lifecycle remain application policy. A client
  migrating off a direct substrate dependency takes these from here rather
  than reimplementing the records.
  Recorded-daemon control is facade-owned rather than reached through the
  substrate's broker client: `DaemonIdentity::verify_live` and
  `verify_for_control` re-check boot, liveness, executable path, and BLAKE3
  digest over this crate's host process facade, and the returned
  `VerifiedDaemon` terminates only the verified process generation and
  answers `has_exited` through the retained process reference, reporting a
  failed observation as an error rather than as an exit.
  `DaemonIdentityRecord` assembles or inspects an identity field by field; the
  sidecar JSON and probe reply bytes are unchanged and pinned by tests.
- `broker_client` (feature `broker-client`, opt-in and outside `full`): the
  broker client adapter. `connect_backend` wraps the substrate's frozen v1
  Hello/Hello-skip connect and returns an owned `std::io` stream;
  `BackendRoute`, `RefusalCode` (total `i32` conversion), `RefusalKind`,
  `BrokerRefusal`, and `BrokerClientError` are owned values, with backend
  errors converted privately. The broker implementation, its wire, and its
  round trips stay in `running-process`; endpoint naming, payloads, and
  fallback policy remain application policy.
- `broker_v2` (feature `broker-v2`, opt-in and outside `full`): the broker
  v2 daemon lifecycle. `backend_endpoint`, `daemon_identity_path`,
  `publish` (a `Publication` guard), `published`, and
  `BrokerLaunch::from_env` own the daemon side; `negotiate`, `connect`, and
  `connect_published` own the client side and return an owned
  `Connection` stream with its `Route`. Identity bytes, the v2 Hello wire,
  name validation, and the identity path stay in `running-process`; daemon
  start policy and payload protocol remain application policy.

Client CI installs the two Dylints in [DYLINT.md](DYLINT.md). They deny direct
implementation-crate use and host `cfg` selection outside this HAL, including
platform branches elided on the CI host.

First-party builds use Soldr's cache-enabled Cargo front door. That preserves
native `CC`/`CXX` caching and Soldr's exclusive resource gate for oversized C,
C++, and published-Rust amalgamations. This scheduling contract complements
the facade boundary: Soldr protects the compilation unit, while the Dylint
prevents a client from introducing a second owner for it. Client workspace
profiles keep the centralized facade at one rustc codegen unit because
dependency-local profile settings do not control a consuming workspace.

Applications retain product policy: which environment variable enables a
facility, where dumps are stored, and which endpoints are exposed.

## Dependency direction

`running-process` is the lower native/process implementation layer and
`kernal-api` is its higher semantic facade. That private dependency has landed:
it is a mandatory, non-optional dependency of the current release, so the
client-side ban is live rather than pending. The reverse dependency is
forbidden. First-party applications migrate to a single direct systems
dependency on `kernal-api` and may not expose backend types in their public
APIs.

Migration branches may temporarily carry both dependencies while a capability
is moved. The corresponding strict Dylint rule is enabled as soon as the facade
has parity, and release branches must not retain the legacy direct dependency.
Exact backend versions are selected by `kernal-api`, not by its clients.
