# Lock, wait, process and IPC inventory

The public and internal wait surface that issue #433 must make bounded.
Each row records the owner, whether a native thread blocks, the current time
policy and what the issue requires. Line references are to `main` when this
file was last updated. Keep the file in step with the code: a slice that
closes a row updates it in the same PR.

Status key:

- **bounded:** the caller must supply a budget, or the wait is finite by construction.
- **optional:** a budget exists but `None` means no deadline.
- **unbounded:** no budget parameter; the wait can last forever.
- **fixed:** a closed #433 slice.

## Async engine (`src/async_engine.rs`, `src/async_engine/`)

| API | Blocks a native thread | Status | Required result |
| --- | --- | --- | --- |
| `Runtime::run` | yes (`block_on`) | unbounded; bounded `Runtime::run_within` (`DeadlineElapsed`, root future dropped on expiry) exists | Migrate callers, then remove the unbounded form. |
| `Task<T>: Future`, `join`, `TaskGroup::join_next` | no | `join_next` **deprecated** (#433); others unbounded; bounded `Task::join_within` (task keeps running on expiry) and `TaskGroup::join_next_within` (`JoinTimedOut`, no result lost) and `join_within` (drops both futures on expiry) exist | Migrate callers, then remove unbounded awaiting. |
| `launch_blocking` / `RuntimeHandle::launch_blocking` | lane thread | unbounded | Timing out the await cannot stop the closure. Document that, and bound the closures callers pass. |
| `Semaphore::acquire`, `acquire_many` | no | **deprecated** (#433); the one internal use is the wasm hub's cancellable process-job join (service lifetime); bounded `acquire_within` / `acquire_many_within(.., timeout)` exist (`SemaphoreAcquireTimedOut`, an expired waiter holds no permits) | Migrate zccache (`cache_system.rs`, `handle_compile_multi.rs`, `handle_link.rs`, `compile_progress.rs`), then remove the unbounded forms. |
| `Sender::send`, `Receiver::recv` (bounded channel) | no | `send` and `recv` **deprecated** (#433), no internal callers; bounded `send_within` (`SendTimeoutError` returns the value, no reservation kept) and `recv_within` (`RecvTimeoutError::{TimedOut, Disconnected}`) exist | Migrate callers, then remove the unbounded forms. |
| `Sender::blocking_send` | yes | **fixed**: required `timeout`; `TimedOut(value)` returns the unsent value and keeps no reservation | none |
| `UnboundedReceiver::recv`, `BroadcastReceiver::recv`, `OneshotReceiver: Future` | no | `recv` forms **deprecated** (#433); `OneshotReceiver` awaiting unbounded; bounded `UnboundedReceiver::recv_within` (`RecvTimeoutError`) and `BroadcastReceiver::recv_within` (`BroadcastRecvTimeoutError`, lag and closure kept) and `OneshotReceiver::recv_within` (`RecvTimeoutError`, reusable after expiry) exist | Remove the unbounded forms once callers migrate. |
| `Notify::notified`, `owned_notified` | no | unbounded; bounded `Notified::wait_within` / `OwnedNotified::wait_within` (`NotifyTimedOut`) exist and keep the enable-before-unlock pattern | Migrate callers, then remove unbounded awaiting. |
| `Mutex::lock`, `lock_owned` (`mutex.rs`) | no | **deprecated** (#433), no internal callers; hands out guards that cross `.await`; bounded `lock_within` / `lock_owned_within` (`LockTimedOut`) exist | Acquisition budget done; still needed: state callbacks returning owned data, opaque permits for exclusion across `.await`, hold watchdog, removal of the unbounded forms. |
| `Mutex::blocking_lock` | yes | **deprecated** (#433), no internal callers; bounded `blocking_lock_within` (`LockTimedOut`) exists | Migrate callers; add a hold watchdog. |
| `RwLock::read`, `write`, `read_owned`, `write_owned` (`rw_lock.rs`) | no | **deprecated** (#433), no internal callers; hands out guards; bounded `read_within` / `write_within` / `read_owned_within` / `write_owned_within` (`LockTimedOut`) exist | As `Mutex`. |
| `RwLock::blocking_read`, `blocking_write` | yes | **deprecated** (#433), no internal callers; bounded `blocking_read_within` / `blocking_write_within` (`LockTimedOut`) exist | Migrate callers; add a hold watchdog. |
| `wait_for_interrupt`, `InterruptSignal::wait`, signal `recv` | no | unbounded by design | Classify as a service-lifetime listener. |
| `sleep`, `sleep_until`, `Interval::tick` | no | bounded | none |
| `timeout`, `timeout_at`, `Deadline`, `cancellable`, `connection_timeout`, `connection_until`, `progress_timeout` | no | bounded | Building blocks the bounded APIs above should use. |

## Process lifecycle

| API | Blocks a native thread | Status | Required result |
| --- | --- | --- | --- |
| `run_bounded_command`, `run_bounded_command_async` (`src/lib.rs`) | yes / lane | **fixed**: deadline required; `None` is refused with `InvalidInput` before spawning (the signature still takes `impl Into<Option<Duration>>` so `Some(budget)` callers compile) | none |
| `PlatformChild::wait`, `kill` (`src/lib.rs`) | no | `wait` **deprecated** (#433), no internal callers; bounded `wait_within(timeout)` exists (`TimedOut`, child left running and killable) | Migrate callers, bound `kill`'s reap, then remove the unbounded `wait`. |
| `PlatformChild::wait_with_output_bounded` | no | **deprecated** (#433), no internal callers; output bounded, time unbounded (waits for natural exit and EOF); bounded `wait_with_output_bounded_within` (kills the direct child on expiry, `TimedOut`) exists | Descendant-held-pipe policy; migrate callers, then remove the unbounded form. |
| `ProcessSession` post-exit drain | no | optional (`ProcessPostExitDrain::WaitForEof` vs `AbandonAfter`) | Remove the unbounded default from ordinary product use. |
| `platform::process::DaemonChild::wait`, `SpawnedChild::wait` | yes | **deprecated** (#433), no internal callers; bounded `wait_within` (polls `try_wait`, `TimedOut`, child left running) exists | Migrate callers, then remove the unbounded form. |
| `sync_spawn_group::SpawnedInner::wait` (Unix) | yes | unbounded, but the child lock is no longer held while blocked; callers bound it through `DaemonChild`/`SpawnedChild::wait_within`, which polls `try_wait` (#476) | **fixed** (#448): `kill` is no longer blocked by a concurrent `wait`. Remove the unbounded facade `wait` once callers migrate. |
| `platform_win::sync_spawn` `wait` | yes | unbounded; callers bound it through `DaemonChild`/`SpawnedChild::wait_within`, which polls `try_wait` (#476) | Remove the unbounded facade `wait` once callers migrate. |
| `SpawnHandle::wait(timeout, cancelled)` (`independent_spawn.rs`) | yes | bounded | none |

## File locks (`src/platform/fs.rs`)

| API | Blocks a native thread | Status | Required result |
| --- | --- | --- | --- |
| `lock_exclusive`, `lock_shared`, `lock_exclusive_owned`, `lock_shared_owned` | yes (`flock` / `LockFileEx`) | **deprecated** (#433), no internal callers; bounded replacements `lock_{exclusive,shared}{,_owned}_for(file, timeout)` exist (`TimedOut` on expiry, owned forms keep the handle across retries) | Migrate zccache's direct callers (`staged_lock.rs`, `download_client/artifact/lock.rs`, `object_store.rs`, test helpers) and its `wait::lock_*_for` wrappers, then remove the unbounded forms. |
| `try_lock_exclusive`, `try_lock_shared` and the `_owned` variants | no | bounded (non-blocking) | none |

## Webview (`src/tauri.rs`)

| API | Blocks a native thread | Status | Required result |
| --- | --- | --- | --- |
| `WebviewHandle::wait_until_loaded`, `wait_until_terminal` | no | bounded (required `Duration`) | none |
| `WebviewHandle::wait_for_terminal` | no | service lifetime by design (#198): an interactive window has no lifetime deadline; the wait owns no timer, is cancellable by dropping it, and ends on closure, cancellation or a terminal host event; `wait_until_terminal(timeout)` is the bounded form | Exception: not deprecated and not linted. |
| `ExternalWebviewHost::run` | UI thread | unbounded by design | Event-loop owner; classify as a service-lifetime exception. |

## Internal waits

| Site | Status | Required result |
| --- | --- | --- |
| `platform::terminal_input::wait_for_terminal_input_event` | optional (`timeout: Option<Duration>`); bounded `TerminalInputCore::wait_for_event_within` (`Timeout`) exists | Migrate callers, then require a budget or document the listener exception. |
| `platform::ipc` and `broker_*` | audited; see [IPC and broker](#ipc-and-broker) | Bound the client paths listed there. |

## IPC and broker

Audited from source (Linux tree unless noted; Windows and macOS mirror it).
Client connect, read and write paths are the hang risk: a dead or wedged
peer blocks them forever. Accept-side waits are service-lifetime listeners.

| API | Blocks a native thread | Status | Required result |
| --- | --- | --- | --- |
| `broker_client::connect_backend` | yes | partly bounded: broker Hello (internal 30 s default) and handoff (`handoff_ready_timeout`, 2 s) are bounded; the final backend connect, and the cached-endpoint attempt, are not (both inside `running-process`) | One caller-supplied deadline over the whole call; needs an upstream `running-process` connect budget. |
| `broker_client::BackendConnection` `Read` / `Write` | yes | read optional (`set_read_timeout`, default `None`); write unbounded | Bound reads by default; a write budget. |
| `broker_v2::connect` | yes | bounded: `deadline` covers negotiation and the dial (`Stream::connect_within` with the remainder) | None. |
| `broker_v2::connect_published` | yes | **deprecated** (#433): unbounded (`Stream::connect`); use `connect_published_within(service, timeout)`; no internal callers remain | Remove in the next breaking release. |
| `broker_v2::negotiate` | yes | bounded (`deadline`) | None. |
| `broker_v2::Connection` `Read` | yes | optional on Unix (`set_read_timeout`, default `None`); unbounded on Windows | Bound by default; a Windows read budget. |
| `broker_v2::Connection` `Write` | yes | optional on Unix (`set_write_timeout`, default `None`); unbounded on Windows | Bound by default; a Windows write budget. |
| `platform::ipc::Stream::connect` | yes | **deprecated** (#433), only internal caller is the deprecated `connect_published` dial; unbounded; on Windows the local-socket layer waits on a busy pipe without limit; bounded `Stream::connect_within(endpoint, timeout)` exists (`TimedOut`) | Use it in `broker_v2` and `broker_client`, then migrate callers. |
| `platform::ipc::Stream` `Read` | yes | optional on Unix (`set_recv_timeout`, default `None`); on Windows named pipes reject I/O timeouts (`Unsupported`), so reads are unbounded there | Bound by default; a Windows read budget (overlapped I/O or a watchdog). |
| `platform::ipc::Stream` `Write` / `flush` | yes | optional on Unix (`set_send_timeout`, default `None`); unbounded on Windows (`Unsupported`) | Bound by default; a Windows write budget. |
| `platform::ipc::Stream::transfer_to_backend` | yes | non-blocking on Linux (`MSG_DONTWAIT`); Windows not yet checked | Confirm on Windows. |
| `AsyncStream::connect`, `read`, `read_exact`, `write_all`, `flush`, `shutdown`, and the `AsyncReadHalf` / `AsyncWriteHalf` forms | no | **deprecated** (#433), no internal callers outside contract tests; every one has a bounded `_within` form on `AsyncStream`, `AsyncReadHalf` and `AsyncWriteHalf` (`TimedOut`) | Migrate callers, then remove the unbounded forms. |
| `LocalSocketStream::connect` (owner-only) | no | unbounded; callers own deadlines | A connect budget. |
| `LocalPipeClient::open` (Windows) | yes (one syscall) | bounded by design: pipe-busy is returned, not waited on | None. |
| `legacy_send_fd_to` | yes | connect unbounded (send is non-blocking) | Bound the connect or remove the deprecated function. |
| `legacy_send_fd_over`, `legacy_duplicate_handle` | yes (one syscall) | bounded by design | None. |
| `Listener::accept`, `InheritedListener` accept, `AsyncListener::accept`, `LocalSocketListener::accept`, `OwnerOnlyPipeInstance::connect` (Windows) | `Listener` yes, others no | unbounded by design; the async forms are cancellable and keep the listener usable | Classify as service-lifetime listeners. |

## Enforcement

`kernal_api_unbounded_wait` (in the `kernal_api_boundary` Dylint library)
denies client calls to every unbounded row above that has a bounded
replacement. Its `UNBOUNDED_WAITS` list must change in the same PR as a row
here. `kernal_api_guard_across_await` denies client `async` bodies that hold a
kernal-api `Mutex` / `RwLock` guard across an `.await`, including a guard
nested in an `Option`, a tuple or a local struct. `kernal_api_blocking_in_async` denies blocking kernal-api calls made
directly in client `async` bodies. Inside `kernal-api`, `#[deprecated]` on
each unbounded form plus `warnings = "deny"` keeps its own code off them;
the remaining uses are contract tests and the documented service-lifetime
exceptions above. The lints do not cover the awaited futures (`Task`,
`OneshotReceiver`, `Notified`). Those Dylints are
still outstanding under #433.
