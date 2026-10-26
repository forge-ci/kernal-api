# Issue 17 implementation status

The blob implementation on this development branch is incomplete and is not
release evidence for issue #17. The generated ABI exposes blob creation,
bounded writes, pull reads, explicit EOF, and closure through the existing operation family, exercised
by the threaded smoke guest. The write import validates the guest-memory range
and checks authority/chunk/pending-byte quotas before copying into host-owned
bytes using atomic byte loads. It retains no guest pointer across suspension.
Read collection validates a caller-supplied destination on each call and
copies only a completed bounded result. Pending calls retain no pointer;
wrong-owner, wrong-kind, short-buffer, and double collection are rejected.
The real guest verifies 64 MiB through 1,024 sequential write/read pairs,
reusing two 64 KiB arrays and checking every returned byte. Generated
`OutputFile::write_blob` dispatches an opaque blob/output token pair through
the same operation protocol to the supplied runtime's blocking lane. Its
wire-level host test verifies exact replacement and rejection of forged or
wrong-owner output grants. The in-process `execute_threaded_root_with_output`
entry point authorizes the destination before root Store creation. The root
guest discovers it through `OutputFile::granted`; child Stores do not receive
an initial grant. The real guest replaces an existing output with the exact
bytes `guest exact output`, with no sibling temporary remaining afterward.
The killable worker protocol now delivers the exact-output grant too: parent
staging is published only after successful execution and reap, with Linux
success, trap, cancellation, and publication-failure proofs described below.
These implementations are not full release acceptance: fresh all-six-target
exact-output evidence and kernel-blocked filesystem behavior remain gaps.
The renamed-parent cleanup regression has native Linux, Windows ARM64, and
macOS ARM evidence recorded below, but that partial coverage does not replace
the full platform acceptance matrix.

Guest `seal` publishes EOF without revoking the readable handle. Pending empty
reads then complete with zero bytes; an empty unsealed blob stays pending.
Producers must await preceding writes before sealing, because sealing rejects
pending and subsequent writes.

## Public guest blob Drop

The public guest `Blob` now revokes its owned host blob on Drop through
generated synchronous opcode 19. It neither suspends nor allocates an operation
slot, so a full pending-operation table cannot prevent cleanup. Validation of
the exact Store owner and blob kind occurs under the same mutex as generation
revocation. Pending borrowing operations receive `Closed`; their guards still
own collection or abandonment of those terminal results. Drop after successful
explicit close or output commit is harmless. Older hosts do not implement this
additive opcode; matching exact pre-1.0 host/guest pins remain required.

The focused host test was RED before the abandonment method existed and now
proves cleanup at operation quota, foreign/non-blob rejection, stale rejection,
released transfer capacity, and subsequent resource creation. The real threaded
guest creates and drops 128 blobs before its existing 64 MiB transfer, exceeding
the live-resource quota without explicit close. Its in-process proof passes
with 2,227 consumed operation results and no final resource/buffer counters.
The added creates account for 128 additional suspensions/results; Drop itself
adds neither. The artifact's only manifest change is `wasi_thread_start` index
374 to 375; the closed imports, signatures, and limits are unchanged. This does
not establish six-native-target acceptance for this latest change.
The same rebuilt artifact also passes both killable-worker execution/output
tests and the forced-output-cleanup test on Linux x86-64.

## Verified host behavior

- Bounded chunk writes and pull reads use the existing scoped resource table.
- Pending writes wait for capacity; reads and resource closure release it.
- Pending reads distinguish data availability, explicit EOF, and cancellation.
- Cancellation discards pending write input. Sketch teardown also discards
  completed, unconsumed read buffers and rejects subsequent result retrieval.
- Exact output grants retain a canonical parent and final filename on the host.
  The requested destination must already be traversal-free: a `ParentDir`
  component is rejected before canonicalization, so `approved/../result.png`
  cannot silently broaden a configured exact-output grant.
  The current synchronous helper requires a sealed blob, writes a sibling
  temporary file, syncs and closes it, and uses the existing atomic replacement.
- Replacement failure removes the owned temporary file. Failed exclusive
  creation does not remove a file belonging to another creator.
- Output jobs check operation/resource authority before starting, before
  each bounded pull, and before sync. Cancellation before job start leaves
  the source blob unconsumed. Cancellation during a syscall cannot interrupt
  that syscall, but the next checkpoint stops further work. Atomic replacement
  still checks the terminal winner under the hub lock.
- Test-only error checkpoints now exercise the actual async output job after
  its first written chunk, before sync, and before atomic replacement. Each
  path returns `Rejected`, preserves the existing final file, removes its
  temporary, and reaches zero resources, operations, output jobs, and retained
  transfer capacity after teardown/join. These are injected boundary errors,
  not failures induced in native filesystem drivers.
- An ownership guard now closes and removes the temporary file on unwind as
  well as ordinary errors. Focused injected-panic tests cover partial writes
  and the post-sync/close, pre-replacement boundary while preserving the
  original final file. This does not establish cleanup after process kill;
  worker-owned staging and parent cleanup remain required for that case.
  All 47 hub tests pass; the existing in-process 64 MiB Cargo guest and its
  exact-output commit also passed (8.23 seconds on Linux x86-64).

Focused command:

```sh
soldr cargo test --locked --features wasm-sketch-host operations::tests --lib
```

The native hub test and the Cargo-built guest both transfer 64 MiB. Run the
guest proof with `CARGO_TARGET_DIR="$PWD/target/threaded-proof" python3
scripts/build_threaded_smoke.py` on every host.
On Linux x86-64 on 2026-09-12, the in-process proof passed in 5.52 seconds and
the worker proof in 9.33 seconds, including compilation/admission overhead
inside each test. The transfer uses an explicit finite fuel budget of
1.7 trillion aggregate and 100 billion for each root/child Store slice.
The guest now also fills the 1 MiB per-blob capacity, submits another 64 KiB
write, and proves that write remains pending across a scheduler turn with no
consumption. One bounded pull then completes the waiting write. It drains all
17 chunks before the 64 MiB round trip. The combined proof therefore asserts
a 1 MiB peak blob buffer; the earlier 64 KiB peak was for the sequential-only
proof. This is a controlled consumer pause in one Store, not a concurrent
multi-producer/consumer stress test. The guest additionally
cancels a capacity-blocked write and an empty pending read, observes typed
`Cancelled` results, and verifies that the cancelled write adds no chunk to
the subsequently drained stream. The generated cancellation import is accepted
only with the complete lifecycle import set. Pending write inputs and
retained read results have separate snapshot counters. This does not establish
a bound on total allocated memory, collection capacity, or simultaneous guest
transfers.

## Remaining acceptance work

### Public Rust guest API migration remains incomplete (#13)

Before this migration, the threaded smoke and screenshot guests depended directly on
`kernal-api-v1-bindings` through source-tree paths in
`guests/threaded-smoke/Cargo.toml` and
`examples/wasm-tauri-screenshot/guest/Cargo.toml`. They called generated operations,
not facade-owned public `kernal_api` guest operations. These unpublished fixtures
prove ABI/runtime behavior only; they do not satisfy #13's public guest API or
exact pre-1.0 consumer-pin acceptance criteria.

Compiler RED evidence at `b95b1c6`: `soldr cargo check --locked
--no-default-features --lib --target wasm32-wasip1-threads -j 1` exits 101.
After compiling Mio 1.2.2, Tokio 1.53.1 rejects its enabled native features:
`Only features sync,macros,io-util,rt,time are supported on wasm.` The diagnostic
is at Tokio's `src/lib.rs:479`; this is a dependency-feature failure, not a
missing Wasm standard library. The check stops before compiling this facade,
so it does not establish what additional source errors remain. Turning off
default features alone is demonstrably insufficient. Target-scoping the native
dependency graph must accompany the source/API split; merely gating native
modules cannot prevent this earlier dependency failure.

The package now selects `kernal_api::guest` for Wasm separately from its existing
native OS selector. Native Tokio, running-process, hashing, memory mapping, and
process inspection dependencies are target-scoped, not made optional for native
consumers. The guest exposes facade-owned errors, blob/output/viewport handles,
and sleep operations around private generated bindings. The screenshot source
fixture now calls that API. The threaded fixture's 64 MiB blob transfer and
exact-output path also use the public API; its separate synthetic-resource and
low-level threading probes deliberately still name generated bindings.

The migrated threaded artifact passes
`supplied_threaded_artifact_admits_and_executes_the_public_profile` with worker
features on Linux x86-64 in 6.09 seconds. Existing assertions retain the 1 MiB
peak buffered-byte budget, 1 MiB plus two 64 KiB chunks of peak retained transfer
capacity, capacity-awaited producer/consumer behavior, typed pending read/write
cancellation, exact final bytes, and zero final resource/operation/buffer counts.
Its exact artifact snapshot changed only compiler-assigned function/type indices:
resolved import/export signatures, memory limits, and the complete internal type
signature multiset were compared and remained identical before snapshot update.
This is public bulk-operation evidence, not yet an exclusively public-host-call
fixture or the outstanding zccache experiment.
The same artifact also passes both worker containment/output tests in 19.22
seconds and `cargo_built_threaded_guest_forced_output_cleanup` in 10.98 seconds.
The latter observes a partial staged file before cancellation, requires forced
containment, and checks reaping, unchanged final output, and staging removal.

The previous compiler RED is GREEN for the no-default Wasm library and native
library checks. All thirteen native facade-policy tests pass, as does strict
Wasm library Clippy. A real release screenshot guest built through Soldr,
received ABI metadata, and passed `actual_screenshot_guest_runs_inside_containment`
on Linux x86-64 in 9.49 seconds. This includes module admission and native
capture/output/teardown assertions, not just adapter compilation.

Cargo packaging initially omitted the nested generated guest package. The ABI
generator now also emits the identical private implementation as
`src/wasm/generated/v1/guest_bindings.rs`, outside that package boundary.
Generated drift checking passes. A 228-file Cargo archive was extracted into
`/tmp/kernal-package-proof.A5BUI0`; a locked no-default Wasm library check using
only the extracted package passes in 6.35 seconds. This verifies source closure,
not publication. The screenshot fixture still uses a migration-only local path
with `version = "=0.1.0"`; replacing that path with a real exact published
guest-capable release remains required before release acceptance.

Acceptance needs a real guest consuming the exact published facade version,
with semantic public handles/errors and no direct generated-binding imports;
then rerun module admission, bounded transfer/backpressure/cancellation, and
native containment proofs through that surface. The representative zccache
experiment and comparative timing gates remain separate required work, not
consequences of the fixture passing.

Worker exact-output success is now wired through
`SketchWorkerConfig::with_output_destination`. The private
`worker::output` component owns a sibling staging directory and
keeps the final destination unchanged until explicit parent commit. Five Linux
tests cover deferred byte-identical publication, partial/completed-file discard,
missing completion, replacement failure, and symlink rejection. Successful
replacement reports directory-cleanup errors separately so they cannot be
mistaken for a failure that preserved the original final file. Staging now
travels with `ExecutionOwnership` through cleanup handoff and reap. A controlled
failed-reap test verifies it stays alive through retry and disappears after
reap without modifying the original final file. The parent transports only the
staged destination and commits after successful execution and reap. A real
Cargo-built threaded guest completed its 64 MiB transfer and exact output in
the worker; the parent published `guest exact output` with no staging leftovers
(Linux x86-64, 10.62 seconds). Both smoke scripts include this ignored artifact
test. Twenty-one worker unit tests pass, including terminal cancellation,
deadline, trap, and forced-containment discard decisions. A feature-gated native
proof now pauses the real worker immediately after its first file write while
the partial file remains open. The harness observes the nonempty partial file,
cancels execution, and requires `ForcedContainment` after grace expires. It
verifies worker/protocol/lease counters are zero, the original final bytes are
unchanged, and the entire staging directory is gone. This passed on Linux
x86-64 in 11.61 seconds. Both smoke scripts explicitly build the test-support
worker and run `cargo_built_threaded_guest_forced_output_cleanup`; ordinary
worker builds contain no pause hook. Native macOS/Windows evidence remains
outstanding, as do failure cases for parent-side filesystem cleanup itself.
An injected parent cleanup-boundary failure exposed an outcome ambiguity:
post-replacement failure previously used the same public code as failed discard.
The facade now reports `worker-output-committed-cleanup` when publication
succeeded, and `worker-output-cleanup` when discard failed without publication.
The RED/GREEN regression checks both error codes and actual final bytes;
all 22 worker unit tests pass. This injection tests error classification, not
a native filesystem driver's cleanup failure or eventual cleanup retry.
The private protocol was version 6 when the optional staging destination was
bounded to 65,536 encoded bytes. Unix bytes and Windows UTF-16 are preserved
without lossy Unicode conversion; relative, NUL-containing, foreign-encoding,
oversized, and truncated inputs are rejected. The worker grants the staged
destination before guest instantiation through the existing opaque output ABI.
All eighteen worker-binary tests, including the twelve protocol tests and
updated grant reconstruction, pass on Linux with the integration enabled.
The Windows-specific unpaired-surrogate test is checked in but not run here.

Version 5 also reserves an optional host-owned URL field for native worker
integration. Its UTF-8 payload is bounded to 16 KiB before allocation;
NUL-containing, oversized, and truncated values are rejected, and absent
authority remains distinct from an invalid empty URL. This is transport
transport for the explicit `SketchWorkerConfig::with_webview_capture` grant.
A worker without `tauri-webview` returns
`native-webview-worker-unavailable` before compiler construction if one is
supplied. A native-enabled worker revalidates the URL and requires the private
staged output, then runs the UI on process main and the root on its own async
runtime. Its UI exit guard also runs on task unwind. Native capture permits up
to 16 processes in the Windows job; ordinary workers retain their one-process
limit. Linux/macOS process groups provide cleanup, not process-count enforcement.
Only native workers receive a platform-owned display/session/loader environment
allowlist; ordinary workers retain the empty environment. No guest chooses these
environment values or process limits.
The worker's 20 unit tests pass both with and without native features on Linux;
the native variant verifies invalid URL and missing-output rejection without
starting UI or compiler work. Strict native-enabled worker Clippy also passes.
The actual screenshot guest also passed inside the Linux worker in 9.34 seconds:
the decoded PNG matches all six fixture color points, the exact output is
published only after successful execution/reap, its neighbor is unchanged, and
parent live worker/task/lease counters are zero. The initial native run failed
with the empty environment; adding only the native allowlist made it pass.
The paired success/trap-after-capture proof then passed in 18.40 seconds. The
real trap is reported as `Execution(Trapped)`, leaves the original output and
neighbor unchanged, removes staging, and drains those same parent counters.
All 18 focused parent lifecycle/configuration unit tests also pass.
The focused Linux CI lane now includes this ignored native proof. This is not
Windows/macOS runtime evidence; the separate forced case follows below.
Worker trace transport is described below.

The source-built `proof-block-after-capture` guest then passed the Linux forced
containment proof in 23.70 seconds. It receives the native snapshot before
waiting indefinitely on an unnotified Rust condition variable. The parent
reports `ForcedContainment { DeadlineExceeded }` after its 20-second deadline
and two-second grace, records one forced reap, preserves the original exact
output and neighbor, removes staging, and has zero live worker/task/lease
counters. Bash and PowerShell build this separate acceptance artifact; normal
guest builds do not enable the fault. CI includes the variant. This establishes
the worker outcome and parent cleanup, not an inventory proving destruction of
every native renderer or a zero-counter report from a forcibly killed guest.
The combined success/trap/block suite passes all three cases in 42.33 seconds;
the focused test passes strict Clippy, and both build scripts produced the
blocking artifact locally (PowerShell was run on Linux, not Windows).

The screenshot CLI now selects this worker path by default, with a sibling
native-enabled executable or explicit `--worker` path. Its Cargo target requires
`wasm-sketch-worker` and `tauri-webview`. Missing workers are errors, never an
in-process fallback. The detailed native timing/failure proofs explicitly select
`--diagnostic-in-process`, accepted only with `tauri-webview-test-support`.
Default-mode CLI success and trap proofs pass on Linux alongside the diagnostic
capture proof (three tests, 26.71 seconds): they verify decoded output or original
file preservation, clean stdout, and zero parent counters in the bounded terminal
summary. The next protocol revision adds the detailed contained trace below.
The full Linux screenshot suite then passed all 15 tests in 123.76 seconds,
including the explicit missing-worker/no-fallback regression, diagnostic load
timeout and write failure, contained success/trap/block, and validator negative
controls. The production CLI configuration also passes strict Clippy without
the diagnostic feature enabled.
The non-worker native test configuration passes strict Clippy too; it exposed
and fixed an over-broad gate on native worker environment helpers. Those helpers
now require both the native capability and worker support.

Protocol version 6 adds one optional acceptance trace batch, capped at 64 KiB
and restricted to printable ASCII/newlines. Oversized trace headers are rejected
before payload allocation. The parent requires the matching execution ack,
rejects duplicate/wrong-request traces, and bounds its entire response producer
to four messages plus one failure notification. The flood regression proves
the producer stops without reading the remaining input or waiting for a consumer;
wrong-direction module payloads are never queued. A feature-gated recorder retains
one batch, with no caller callback or file I/O on the supervisor thread.

Native test workers send their bounded ABI/callback trace after joined execution,
including post-drop compiler counters. The default CLI prints it only after the
worker terminal path. All three default CLI tests pass in 20.78 seconds, now
requiring the detailed generated-boundary, >=5-second load/capture, and zero-hub/
compiler-resource assertions inside containment. Forty focused worker/protocol
unit tests and strict combined Clippy pass. This is terminal-batch transport,
not live streaming: forced termination before emission still loses worker-local
events and must not be presented as a zero-counter report from the killed worker.
The non-native worker's 21 binary/protocol tests and four containment regressions
also pass with version 6. The acceptance recorder test verifies destructive
retrieval and duplicate/request rejection; final formatting and diff checks pass.

The real redirect and production 30-second load-timeout proofs now use the
default contained CLI, not diagnostic in-process execution. Both pass with the
admission control (three tests, 39.79 seconds): the guest retains its distinct
load-rejected/load-timed-out status, no capture is requested, the original output
and neighbor are unchanged, staging is absent, and the transported hub/compiler
trace plus parent worker/task/lease counters are zero. The old unreachable
in-process load-failure assertions were removed rather than counting two modes
as separate acceptance evidence.

A real contained publication-failure proof passes on Linux in 9.29 seconds.
After the guest's HTTP request establishes that grants/staging exist, the fixture
preserves its original output under another name and places a nonempty directory
at the final destination. The unchanged actual guest completes native capture,
its private staged write, and close; final atomic replacement then fails with
`worker-output-commit`. The transported execution/timing/zero-resource trace
passes, parent counters drain, staging disappears, and the preserved original,
neighbor, and obstructing directory contents are unchanged. This is a native
replacement failure, not a permissions/disk-exhaustion or renamed-parent proof.

A full-branch single-agent pre-push review identified a parent publication race:
the supervisor sampled stop state before flushing staged output, allowing a stop
during that flush to be ignored. A deterministic post-sync cancellation hook
reproduced `Completed` instead of `Stopped(Cancelled)`. Publication now checks
live stop authority again immediately before attempting replacement and
explicitly discards staging when stopped. Cancellation and simulated deadline
boundary tests preserve the original; discard failure retains the distinct
`OutputCleanup` result rather than claiming publication occurred. All 41
worker/protocol tests and strict worker-feature Clippy pass. This is a boundary
check, not rollback after replacement starts or a hard bound on parent-owned
filesystem syscalls. The review's two stale-documentation findings were also
corrected; all-six-native-target acceptance remains incomplete.
The four real default-CLI regressions pass after the change in 30.87 seconds:
missing worker, publication failure, standalone build/capture, and trap output
preservation. Formatting and diff checks also pass.

A real contained native-capture failure proof passes on Linux x86-64 in
9.60 seconds. The fixture renders fixed-seed RGB noise across the ordinary
800×600 viewport; the native PNG encoder exceeds its unchanged 1 MiB limit.
The unchanged guest reports capture-rejected (exit 97), never submits output
commit, and preserves the original output and neighbor with no staging left.
The bounded acceptance trace explicitly records `capture-encoded-byte-limit`
from the native request error, then verifies zero hub/compiler counters; parent
worker/task/lease counters also drain without force. The first test run failed
because the trace could not distinguish this cause from unrelated capture
errors. The marker is test-feature-only and does not change the guest ABI or
production capture behavior. This proves native encoder quota failure, not
arbitrary OS capture-device failure or native execution on the other targets.

The five direct contained-worker proofs now honor the same CI proof-directory
setting as the CLI harness. They retain scenario-specific output directories,
raw available worker traces, and parent process JSON before assertions. The
JSON includes OS/architecture, terminal category, guest exit code, counters,
and an explicit worker-trace availability flag. Only fully passing assertions
write `validation.txt`. A forced-kill proof therefore retains truthful parent
evidence without manufacturing a worker trace or claiming worker-local zeros.
These files fall under the existing always-upload CI artifact path.
All five final-format proofs pass in 70.01 seconds on Linux x86-64. A separate
on-disk check verifies exactly five validation markers, zero parent live
counters, quota-failure exit 97, and trace presence matching the JSON flag
(four traces, none for forced termination). Strict focused Clippy, formatting,
and diff checks pass.

A real contained cancellation-during-load proof exposed a cooperative shutdown
bug: epoch interruption did not wake a guest suspended in the generated async
`operation_yield` host import, so the parent had to force containment. The epoch
broker now weakly tracks the root's shared operation hub and revokes it after
publishing cancellation/deadline, outside its registration lock. Revocation
uses a nonblocking hub-lock attempt and retries on subsequent ticks, so a
stalled filesystem replacement cannot stall the shared ticker. This wakes
operation waiters and cancels producers without changing the guest ABI. Binding
after an interruption is covered too. The actual native regression passes in
4.90 seconds with `Stopped(Cancelled)`, no forced termination, zero transported
hub/compiler and parent worker/task/lease counters, no capture, and unchanged
output/neighbor with no staging. A unit test covers both cancellation and
deadline before/after hub binding. This does not make native atomic waits
cooperatively interruptible; the block-after-capture proof still requires force.
The full 17-test native screenshot suite passes (146.41 seconds) with the
initial wakeup fix; the nonblocking refinement passes ten epoch-focused tests
and strict combined-feature Clippy; its native cancellation rerun passes in
4.54 seconds. A broader worker-feature library run passes
345 tests and fails the pre-existing GNU build-ID assertion: `readelf -n` confirms
this Soldr-produced test executable lacks a GNU build-ID note. An explicit
Linux test link, `soldr cargo rustc --locked --features wasm-sketch-worker --lib
--profile test -j 1 -- -C link-arg=-Wl,--build-id=sha1`, produces a GNU build-ID
note verified by `readelf -n`; running that test executable passes all 346 tests
in 4.10 seconds, without changing or skipping any assertion. The ordinary local
link configuration still lacks the note, so this does not claim the unmodified
`cargo test` command is green in that configuration.

The default-contained CLI success proof now omits `--module` and does not
require an artifact environment variable. Its first run exposed strict Soldr
reentry rejection when the harness was launched by a still-running
`soldr cargo test`. CI now builds the harness through Soldr with Cargo JSON,
requires exactly one matching test executable, then launches that executable
after Soldr exits. No guard variables are removed or disabled. The README also
launches the built CLI directly rather than nesting `soldr cargo run` around
the CLI's own Soldr guest build. The real build/admission/contained-capture proof
passes in 9.88 seconds, including PNG/timing/zero-counter/output assertions.
It has a separate five-minute build-and-execution proof bound; prebuilt native
failure cases retain their 90-second bound. This is Linux x86-64 evidence, not
the missing five native target proofs or the #13 comparative timing experiment.
Strict focused Clippy, formatting, diff checks, and workflow actionlint pass.
Actionlint also caught invalid job-level `runner.temp` references; the screenshot
job now exports its storage paths from a runner setup step. The existing popup
fixture explicitly documents its intentional child-shell variable expansion.

The generated guest yield facade now accepts only the host's success sentinel
`1`. A native scalar-import regression reproduced `-1` incorrectly returning
success before the fix; it now verifies success, failure, zero, and unknown
responses through the checked-in generated guest facade. All eight generator
tests, strict all-target generator Clippy, and generated-artifact drift checks
pass. This is error-boundary coverage, not evidence of worker output support.

Strict lint validation passes for `soldr cargo clippy --locked --features
wasm-sketch-worker --all-targets -- -D warnings` and the host-only library
variant (`--features wasm-sketch-host --lib`). Worker-only helpers now match
their consumers' feature gates.

The no-default dependency graph is **not** acceptance-complete: `soldr cargo
tree --locked --no-default-features -e normal --prefix none` still includes
`png` and `x11rb` through mandatory `running-process-platform-internal`
4.10.10. Its Linux dependency declarations are unconditional, as already
noted in `Cargo.toml`; this requires an upstream feature-gating release, not
a facade-only optional-dependency change. The manifest isolation test alone
does not prove this transitive graph requirement.
The upstream fix was reviewed and merged in
[running-process#1201](https://github.com/zackees/running-process/pull/1201)
on 2026-09-12 (merge commit `0e39d9d403883dc87c71d677012bc0c0a5d0c693`).
The resolver guard reproduced the
PNG/X11 failure before the change and passes afterward for the host and both
Windows targets. Upstream defaults retain icon support; explicit
`kernel-substrate` excludes it. Nine guard tests, 21 existing icon tests,
default/substrate/icon-enabled builds, and strict platform all-target Clippy
pass on Linux. Bare platform compilation also passes, with two existing
unused process-snapshot warnings. Review identified an integration-test import
that also needed the icon feature gate; the corrected PTY test target compiles
with `kernel-substrate,pty` both without and with `window-icon`. The PR was
admin-merged on this local validation, without waiting for GHA. Native macOS
and Windows execution was not performed for this change. An upstream registry
release and this repository's registry-pin update remain outstanding; the merge
alone does not fix the currently pinned dependency graph. No path patch was added.

The shared hub now enforces independent live-blob, pending-read, and
pending-write count limits (128 each by default). Hosts configure these and
chunk/per-blob/per-sketch byte limits through facade-owned `SketchBlobLimits`
and `SketchExecutionLimits::with_blob_limits`. Zero count limits disable the
corresponding admission. Byte limits must be nonzero and ordered
chunk <= blob <= sketch, with chunk lengths representable in the generated
32-bit transfer fields. Blob creation checks include reserved generated creations.
Pending-write rejection occurs before copying input; cancellation frees the
pending-I/O count even before the terminal result is collected. The separate
operation-table limit still accounts for that uncollected result. Focused
tests cover count reuse after cancellation/close and eight simultaneous host
submissions competing for one pending-write slot. The real threaded guest now
also makes each of its two child Stores create, write, read, seal, and close a
separate 64 KiB blob through the public generated operation API. Its supplied
artifact proof grants exactly two live blobs, pending reads, and pending writes;
it records both child-stream completions and verifies that every resource and
operation counter drains to zero. Each child fills its independent 1 MiB blob,
proves a seventeenth write remains pending across a scheduler turn, cancels a
second pending write, then releases the first only after a bounded read. This
establishes bounded backpressure and cancellation through real guest-thread
streams; the existing root 64 MiB proof remains the total-transfer evidence.
The same admitted artifact passes execution and parent-owned-output commit
inside the killable worker, forced-output cancellation cleanup, and the native
parent-death worker-reap proof with those two-child limits. This extends the
guest-stream evidence through the containment boundary rather than treating an
in-process result as a worker guarantee. A one-second worker deadline during
the actual child-stream pressure workload stops the guest and releases the
parent state; it is never misclassified as a completed run. The terminal is
usually `ForcedContainment { DeadlineExceeded }`, but when the epoch deadline
lands where the child can stop cooperatively it is
`Stopped(DeadlineExceeded)` (#274). Forced cleanup of an open output is proven
deterministically by `cargo_built_threaded_guest_forced_output_cleanup`.
All 42 hub tests pass. With the count limits enabled, the existing 64 MiB
Cargo guest passed on Linux x86-64 in-process (5.87 seconds) and inside the
killable worker (9.18 seconds), reusing the admitted artifact and rebuilding
the host.

The worker supervisor sends all seven capacity limits plus the exact
seconds/nanoseconds representation of the Blob transfer idle budget, and the
worker validates them through the same public constructor before compiler
construction. Private worker protocol version 7 rejects older peers rather
than silently using default limits; rebuild the worker executable together with
the host. This changes no guest ABI import signature or guest artifact. Pending
inputs and completed reads still have separate byte budgets. Hosts can additionally set
`SketchBlobLimits::with_maximum_transfer_bytes` for combined hub-owned blob,
pending-input, and completed-read backing capacities. Its default is three
times the sketch-storage budget plus one chunk; the minimum configurable
value is the sketch-storage budget plus two chunks. Writes preserve one chunk
of pull headroom, and reads can use that reserve. Result collection releases
capacity and revisits waiting writers. The bound includes write/read copy
overlap. Exact-sized read allocations avoid an eight-byte minimum growth for
smaller chunks. All 43 hub tests pass, including a tight 16-byte combined
budget that rejects input before copying, remains drainable, and resumes the
writer after result collection.

Native pulls now return a private read-only chunk whose backing capacity stays
charged until drop, including chunks held by the output writer during blocking
I/O. Drop frees the buffer before releasing its credits and driving waiting
operations. Teardown revokes resources but does not erase a still-live native
chunk's charge. Plain-`Vec` pull/collection helpers exist only in unit-test
builds; the production generated collector copies synchronously into guest
memory and native output uses the charged chunk. Allocator-internal
reallocation scratch is still outside this capacity ledger, which is not a
total process-memory limit. Root finalization now revokes authority, drains
guest children, and joins retained output jobs before recording final cleanup.
Output jobs have a separate lifetime count and are capped by the operation
limit even after guest cancellation/result consumption. Job completion releases
that count, not cancellation. A join error has the facade-owned
`output-cleanup-failed` result when no earlier execution failure takes priority.
Native blocking syscalls cannot be interrupted in-process: joining a wedged
filesystem operation can wait indefinitely. The required worker-owned output
grant and parent cleanup protocol remain necessary for bounded containment.
Validation now includes 46 hub tests and 25 root-observation tests. Controlled
blocking work proves that joining remains pending while a native chunk is
held, that the job cap rejects additional output admission, and that job and
memory counters reach zero after release. An injected unwind remains visible
to cleanup even after completed task handles are pruned. The reused Cargo
guest passed in-process (6.19 seconds, including exact output) and in the worker
(10.44 seconds including test-side compilation/admission) on Linux x86-64.
Both 64 MiB Cargo guest paths also pass with an explicit 2,228,224-byte
combined hub limit; the in-process proof retains its exact 1,179,648-byte peak
assertion. The version-3 protocol tests, reconstruction test, and host-policy
validation test pass with the additional transfer-budget field.
The non-default policy (64 KiB chunks, 1 MiB per blob, 2 MiB sketch storage,
one live blob/read/write) passed the existing 64 MiB Cargo guest in-process
(6.98 seconds) and in the rebuilt worker (11.07 seconds including test-side
compilation/admission), on Linux x86-64. Protocol round-trip/version rejection
and worker reconstruction of non-default/invalid limits also passed.

The capacity audit now measures `retained_transfer_capacity` separately from
payload lengths. A regression demonstrates why aggregate enforcement is still
required: after writing 1,024 bytes and reading 512 without collection, the hub
retains at least 1,536 bytes of backing capacity (1,024 in the partially drained
blob plus 512 in the result), although payload-length counters total 1,024.
Blob storage growth now checks the sum of retained blob backing capacities
against the sketch byte limit and reserves exactly the requested growth instead
of using geometric growth. Partial drains do not release that capacity budget;
emptying or closing the blob does. A focused regression verifies that another
blob's synchronous write is rejected and its asynchronous write stays pending
until the first blob releases its allocation. All 38 native hub tests pass.
This is a bound on blob storage, not yet one aggregate transfer-memory budget:
pending inputs and completed read results remain separately accounted.
The existing Cargo-built 64 MiB guest also passed with this accounting change
on Linux x86-64: 5.95 seconds in-process and 9.47 seconds through the killable
worker. These runs reused the admitted guest artifact and rebuilt the host.

`peak_retained_transfer_capacity` now retains a high-water mark across teardown.
It samples input admission and storage growth before pending inputs are dropped,
and includes the overlap while a pull allocates its result before releasing
an empty blob's backing buffer. A regression observes a 2,048-byte peak for a
1,024-byte asynchronous write/read even though snapshots after each operation
retain only 1,024 bytes. All 39 focused hub tests pass.
The in-process Cargo guest proof additionally asserts an exact 1,179,648-byte
peak (1 MiB blob storage plus two 64 KiB chunks) during its pressure phase and
zero retained capacity after teardown. That proof passed in 6.03 seconds on
Linux x86-64 with the existing admitted artifact and rebuilt host.

These counters now include native chunk capacity until drop, exposed separately
as `native_transfer_capacity`. All 44 hub tests pass, including retained-native
chunk pressure, drop-triggered writer progress, and truthful accounting across
teardown. Allocator-internal reallocation scratch is excluded; the high-water
mark is not a process-memory peak.
The existing Cargo guest remains green with native chunk charging: 6.24 seconds
in-process (including exact-output commit) and 9.61 seconds through the worker
on Linux x86-64. The admitted artifact was reused and the host rebuilt.

- Extend the sole generated ABI with bounded guest-memory transfers and
  semantic blob/output APIs; retain no guest pointer across suspension.
- Configure and account total bytes, live blobs, pending reads/writes, and all
  in-flight allocations, including retained capacity and completed results.
- Complete ordering and progress-timeout behavior under multiple producers and
  consumers; extend the real child-stream proof to progress-timeout scheduler
  races beyond the worker deadline containment evidence.
- Complete output commit cancellation coverage. Dispatch now uses the caller's
  blocking lane, reserves exclusive blob consumption, and checks the operation
  terminal winner at final replacement under the hub lock. A stalled filesystem
  replacement holds the hub lock; worker containment must bound this case.
- Extend the implemented exact-output worker grant and staging cleanup proofs
  to all supported native targets, retaining only semantic handles in guests.
- Extend the 64 MiB generated guest proof with slow-consumer backpressure,
  cancellation, forged/stale/cross-sketch handles, and teardown.
- Inject write, flush, and rename failures and cancellation at the commit
  boundary; prove preservation of existing output and temporary cleanup.
- Verify the six supported targets and native output behavior on each OS;
  document measured quotas, transfer bytes, peaks, and cleanup counters.
- Review, validate, and merge the completed PR before closing #17.

The wider goal also retains #13, #19–#22, #28, and fp-bindgen#1. Completion of
these native hub tests does not close those deliverables.

## Native CI matrix rollout (not execution evidence)

Current-commit evidence: run
[34731795169](https://github.com/zackees/kernal-api/actions/runs/34731795169)
at `95949ce` passes both native Linux screenshot jobs. The x86-64 job
`103655809596` passes five ordinary tests (0.25 seconds) and all sixteen
native/artifact tests (141.01 seconds); the ARM64 job `103655809612` passes
the same groups in 0.20 and 142.81 seconds. Both explicitly execute and pass
the renamed-parent staging regression and the default-contained CLI success,
trap, missing-worker, and publication-failure cases. Uploaded screenshot
artifacts are `10309592039` (x86-64) and `10309636767` (ARM64). These are
native execution results, not cross-checks; they do not establish Windows or
macOS acceptance on this commit. The rollout notes below retain their original
pre-execution context.

The same run's Windows ARM64 native screenshot job `103655809650` also
passes: five ordinary tests in 1.51 seconds and sixteen native/artifact tests
in 146.19 seconds, including the production renamed-parent cleanup regression.
Artifact `10309795953` was downloaded and inspected. Its
`contained-RenamedParent-CUZUQA/process.json` records Windows/aarch64,
typed nonzero guest exit 113, one spawned and reaped worker, no forced kill,
and zero live workers, protocol tasks, and pending root leases;
`validation.txt` confirms all contained assertions passed. This supplies
native evidence for the production file-ID anchor on Windows ARM64, rather
than only the earlier test-only x86-64 probe. Windows x86-64 screenshot and
macOS current-commit acceptance remain separate gates.

The dedicated screenshot job is now `wasm-tauri-screenshot-native`, with six
explicit runner/target pairs documented in the example README. Each checks
the Rust host triple and builds the test executable with native Cargo defaults;
neither cross-compilation nor an emulated host is counted as native execution.
Linux restores its actual architecture's pkg-config path and uses WebKitGTK
4.1/Xvfb. macOS runs its native harness directly. Windows uses PowerShell guest
builds and Cargo JSON executable selection, verifies or installs the signed
Microsoft Evergreen WebView2 Runtime, and records its version. All Rust tools
still run through Soldr, and harness execution follows the outer Soldr exit.
Each target gets distinct always-upload proof artifacts and fail-fast is off.

Workflow actionlint and syntax parsing of all five PowerShell steps pass
locally. Hosted [run 34787261950](https://github.com/zackees/kernal-api/actions/runs/34787261950)
at `ad11917` completed every `wasm-tauri-screenshot-native` pair successfully:
Linux x86-64/ARM64, macOS x86-64/ARM64, and Windows x86-64/ARM64. This is
native execution evidence for the exact six-target screenshot matrix, not
cross-compilation. It does not make later commits green, replace the remaining
worker-containment evidence, or excuse a future runner, toolchain, GUI-session,
or native-adapter failure; those must remain visible and be resolved or recorded
as infrastructure blockers.

The local explicit-target Linux build failed at linking before native test
execution. Soldr selected its GNU sysroot linker, which reported unresolved
transitive Nix WebKitGTK libraries (including libffi and libepoxy). A repeat
reproduced the link failure. The native-host guard passes, but this build is
not a passing matrix proof. The workflow now omits `--target` for the native
harness while retaining its exact Rust host-triple guard, avoiding an explicit
target's sysroot selection for native GUI libraries. The revised Soldr build
completed locally in 2m46s; its Cargo-JSON-selected harness then passed
`actual_screenshot_guest_native_capture_quota_failure_drains_containment`
under Xvfb in 9.27s (one passed, 17 filtered out). This is focused Linux x86-64
evidence, not a full-suite or other-platform result. The six native execution
requirements are unchanged.

Full Linux x86-64 revalidation at `28204bf` subsequently passed all 18
`wasm_tauri_screenshot` tests with `--include-ignored --nocapture
--test-threads=1` in 148.53s. The native harness was selected from Soldr's
Cargo JSON output and executed under Xvfb after Soldr exited. This includes
the CLI's real guest build, capture/PNG validation, real load timeout,
redirect rejection, capture quota rejection, cancellation, forced blocked
worker teardown, trap cleanup, output publication failure, and negative
controls. Diagnostics are retained locally at
`/tmp/kernal-full-native-proof.h8OJfe`: nine CLI process reports and five
direct containment reports with completed validation markers. All five
direct reports record zero live workers, protocol tasks, and pending root
leases. Forced termination still does not claim a final child-owned trace.
This is local Linux evidence only, not a hosted CI run or completion of #22.

## Renamed-parent staging cleanup

A focused RED regression reproduced `NotFound` from parent-owned staging
discard after renaming the destination's parent. The private filesystem
capability now retains an open scratch-directory handle using optional
`cap-std`, enabled only by `wasm-sketch-worker`. Ownership transfers from
`TempDir` after the handle opens; explicit cleanup and best-effort Drop use
the handle, never TempDir's stale pathname. The Linux regression now passes,
including reuse of the old path with unrelated data that must survive.
A second regression covers cleanup after commit fails following parent rename.
All 29 focused worker unit tests and strict worker-feature Clippy pass.
The default normal dependency tree excludes the new backend; the previously
recorded upstream PNG/X11 isolation gap remains.

This fixes cleanup after an ancestor rename, not every filesystem race:
publication and the worker's output grant remain path-based. The backend
does not promise atomic removal against concurrent directory renames, and
abrupt parent death does not run Drop. Windows/macOS runtime validation
remains required.

Hosted Windows runs exposed a separate limitation: the long-lived `cap-std`
directory handle prevented the destination ancestor from being renamed
(`AccessDenied`), so both renamed-parent unit regressions and the native
screenshot scenario failed before exercising cleanup. The Windows candidate
now keeps a metadata-only, delete-shared handle during execution. Cleanup
resolves that live handle's current path, acquires a restrictive `cap-std`
handle, and compares volume plus 128-bit file identity before removal. A
mismatch fails without deleting the candidate directory. It never passes the
permissive metadata handle to `cap-std`, whose directory constructor requires
delete sharing to be disabled. Linux/macOS retain the existing cfg-free
descriptor backend through their selected platform roots.

The candidate passes Windows x64 cross-compilation and strict Clippy, plus
the seven Linux staging/publication tests. A Windows-only identity mismatch
test is added; the existing renamed-parent tests are unchanged. Native
Windows runtime GREEN is still required before treating this as resolved.

Native result at `b61c48f`: Windows x64 all-feature CI job `103654061802`
in run `34731156181` passed 617 tests but failed both renamed-parent unit
regressions at the rename itself with `AccessDenied` (3 tests ignored).
The metadata-only handle candidate therefore does **not** resolve ancestor
rename pinning. Its identity-mismatch test passes, but that is not evidence
for the missing rename behavior. Keep the regression failures visible while
revisiting directory ownership; neither cross-compilation nor the clean
static review substitutes for this native result.

The follow-up ID-opened-handle experiment **passed natively on Windows x64**
at `c41062b`, run `34731497151`, job `103654994865`. It verified continuous
ownership during handle transfer, ancestor rename, cleanup of the moved
original, and survival of unrelated data at the reused pathname. The run
still failed the two production renamed-parent regressions (618 passed,
2 failed, 3 ignored), because production had not yet adopted that sequence.
The next candidate moves that tested sequence into the Windows constructor;
the test now exercises the production constructor directly. File-ID reopening
must succeed and match the original full identity before ownership transfers;
unsupported filesystems fail rather than reverting to a pathname-only owner.
This does not yet establish production native GREEN or filesystem coverage
beyond the native NTFS experiment.

The actual contained screenshot guest now also passes the renamed-parent
scenario on Linux x86-64 (9.33s). The fixture renames the destination parent
upon the first native HTTP request, after staging/grant creation. The guest
finishes capture and submits output write, reports typed write rejection
(exit 113), and drains its trace counters. Parent reap/lease counters are zero,
the original output and neighbor survive in the moved directory, and no
staging entry remains. This proof is included in the native screenshot matrix;
it does not claim success on unexecuted targets or successful publication
through a moved path.
The follow-up combined-feature screenshot Clippy run did not complete: GTK's
compiler received SIGTERM (Soldr reported zero cgroup OOM kills). Its build
record is `20260913T010933Z-home-niteris-dev-kernal-api.xml` under the local
Soldr build logs. The archived compiler journal confirms signal 15 but does
not identify its sender. After confirming the failed process was terminal,
the unchanged focused Clippy command passed on retry in 20.29s; formatting
and diff checks also passed. The original interruption remains recorded
rather than being attributed to an unproven OOM cause.

## Public transfer cleanup follow-up

Review found that cancellation on guest transfer Drop retained a terminal
operation slot; completed reads could also retain their result bytes. The
candidate now uses generated submit opcode 18 for synchronous transfer
abandonment. Explicit cancellation remains pollable. The host validates Store
ownership and transfer kind before removing the slot; no extra slot or guest
scratch allocation is needed. Output publication now borrows the guest blob
and output wrappers so preflight rejection does not lose the caller's handles.

The 68 focused host operation tests pass, including repeated abandonment with
a one-operation limit, completed-read byte reclamation, foreign-store rejection,
duplicate abandonment, and non-transfer rejection. Four native adapter tests,
strict host-library Clippy, and the default-free Wasm library check also pass.
These checks do not replace actual Wasm execution: the threaded guest now
drops 128 pending reads before its bounded streaming proof, exceeding the host's
64-slot limit. Its rebuilt artifact preserved all signatures and imports while
changing three function indices in the exact snapshot. Its in-process Wasmtime
execution passes in 6.16s, including the 128 dropped reads and 64 MiB streamed
round trip. Both worker-containment tests also pass (19.68s), as does the
forced-output cancellation cleanup test (10.62s). Review and native acceptance of the
complete candidate remain required before merge.

The rebuilt public-facade screenshot guest passes the Linux x86-64 contained
native capture test under Xvfb (9.35s). The packaged 228-file crate was extracted
outside the checkout and passed a locked, default-free
`wasm32-wasip1-threads` library check, proving the private generated support file
is included. All 13 facade-policy tests pass. The existing single reviewer
rechecked both ownership findings and found them resolved with no new actionable
findings. This is local candidate evidence, not six-target native acceptance or
authorization to publish a matching release.

The macOS ARM retry of run `34732458921` completed successfully for prior head
`2722c6c` (job `103660674379`). Its 16 native screenshot cases passed in
139.13s, including renamed-parent cleanup, quota rejection, cancellation, and
trap paths. Abandoned browser connections still produced EINVAL during fixture
setup, but the server discarded those connections and continued serving later
requests. Together with that run's other five successful native jobs, this
completes the six-target screenshot matrix for that prior head only. The
public-guest head `8eeb166` has separate validation in run `34733726389`.

The public guest regression now attempts output commit before sealing, observes
typed rejection, then seals and successfully commits using the same handles.
The in-process Wasmtime proof passes (6.24s). Its exact lifecycle expectation
includes one additional consumed result and at most one additional suspension
for the rejected blocking output job; zero final resource/operation counters
and the existing byte budgets remain unchanged.
The two worker proofs pass in 19.67s, and forced-output cleanup passes in
10.81s. Strict host-library Clippy and formatting checks pass. The same
reviewer independently reran the actual-artifact proof (6.13s) and returned
clean for the regression, counter changes, snapshot, and evidence documents.

## Public guest six-target native evidence

[CI run 34733726389](https://github.com/zackees/kernal-api/actions/runs/34733726389)
completed successfully for `8eeb166a2ee99a88f8c1f3510fa4e465379d931b`.
Each native screenshot job's log reports 6 ordinary tests and 16 native cases
passed, with no failures. The native cases were explicitly run, not counted
as passing merely because the ordinary invocation ignored them.

| Native target | Ordinary tests | Native cases | Native case duration |
| --- | ---: | ---: | ---: |
| Linux x86-64 | 6 | 16 | 155.62s |
| Linux ARM64 | 6 | 16 | 143.08s |
| macOS x86-64 | 6 | 16 | 166.39s |
| macOS ARM64 | 6 | 16 | 137.82s |
| Windows x86-64 | 6 | 16 | 156.75s |
| Windows ARM64 | 6 | 16 | 146.17s |

This validates the public-facade screenshot migration at that exact head.
The later unsealed-output retry regression has separate local Wasm/worker
evidence above; the admission timing example is also later work. Neither is
represented as part of this CI run. Matching release pins, default image
dependency isolation, the representative policy/archive experiments, and
the binding-candidate decision remain outstanding.
