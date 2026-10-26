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
| `Task<T>: Future`, `join`, `TaskGroup::join_next` | no | unbounded; bounded `Task::join_within` (task keeps running on expiry) and `TaskGroup::join_next_within` (`JoinTimedOut`, no result lost) and `join_within` (drops both futures on expiry) exist | Migrate callers, then remove unbounded awaiting. |
| `launch_blocking` / `RuntimeHandle::launch_blocking` | lane thread | unbounded | Timing out the await cannot stop the closure. Document that, and bound the closures callers pass. |
| `Semaphore::acquire`, `acquire_many` | no | unbounded; bounded `acquire_within` / `acquire_many_within(.., timeout)` exist (`SemaphoreAcquireTimedOut`, an expired waiter holds no permits) | Migrate zccache (`cache_system.rs`, `handle_compile_multi.rs`, `handle_link.rs`, `compile_progress.rs`), then remove the unbounded forms. |
| `Sender::send`, `Receiver::recv` (bounded channel) | no | unbounded; bounded `send_within` (`SendTimeoutError` returns the value, no reservation kept) and `recv_within` (`RecvTimeoutError::{TimedOut, Disconnected}`) exist | Migrate callers, then remove the unbounded forms. |
| `Sender::blocking_send` | yes | **fixed**: required `timeout`; `TimedOut(value)` returns the unsent value and keeps no reservation | none |
| `UnboundedReceiver::recv`, `BroadcastReceiver::recv`, `OneshotReceiver: Future` | no | unbounded; bounded `UnboundedReceiver::recv_within` (`RecvTimeoutError`) and `BroadcastReceiver::recv_within` (`BroadcastRecvTimeoutError`, lag and closure kept) and `OneshotReceiver::recv_within` (`RecvTimeoutError`, reusable after expiry) exist | Remove the unbounded forms once callers migrate. |
| `Notify::notified`, `owned_notified` | no | unbounded; bounded `Notified::wait_within` / `OwnedNotified::wait_within` (`NotifyTimedOut`) exist and keep the enable-before-unlock pattern | Migrate callers, then remove unbounded awaiting. |
| `Mutex::lock`, `lock_owned` (`mutex.rs`) | no | unbounded; hands out guards that cross `.await`; bounded `lock_within` / `lock_owned_within` (`LockTimedOut`) exist | Acquisition budget done; still needed: state callbacks returning owned data, opaque permits for exclusion across `.await`, hold watchdog, removal of the unbounded forms. |
| `Mutex::blocking_lock` | yes | unbounded; bounded `blocking_lock_within` (`LockTimedOut`) exists | Migrate callers; add a hold watchdog. |
| `RwLock::read`, `write`, `read_owned`, `write_owned` (`rw_lock.rs`) | no | unbounded; hands out guards; bounded `read_within` / `write_within` / `read_owned_within` / `write_owned_within` (`LockTimedOut`) exist | As `Mutex`. |
| `RwLock::blocking_read`, `blocking_write` | yes | unbounded; bounded `blocking_read_within` / `blocking_write_within` (`LockTimedOut`) exist | Migrate callers; add a hold watchdog. |
| `wait_for_interrupt`, `InterruptSignal::wait`, signal `recv` | no | unbounded by design | Classify as a service-lifetime listener. |
| `sleep`, `sleep_until`, `Interval::tick` | no | bounded | none |
| `timeout`, `timeout_at`, `Deadline`, `cancellable`, `connection_timeout`, `connection_until`, `progress_timeout` | no | bounded | Building blocks the bounded APIs above should use. |

## Process lifecycle

| API | Blocks a native thread | Status | Required result |
| --- | --- | --- | --- |
| `run_bounded_command`, `run_bounded_command_async` (`src/lib.rs`) | yes / lane | **fixed**: deadline required; `None` is refused with `InvalidInput` before spawning (the signature still takes `impl Into<Option<Duration>>` so `Some(budget)` callers compile) | none |
| `PlatformChild::wait`, `kill` (`src/lib.rs`) | no | unbounded; bounded `wait_within(timeout)` exists (`TimedOut`, child left running and killable) | Migrate callers, bound `kill`'s reap, then remove the unbounded `wait`. |
| `PlatformChild::wait_with_output_bounded` | no | output bounded, time unbounded (waits for natural exit and EOF); bounded `wait_with_output_bounded_within` (kills the direct child on expiry, `TimedOut`) exists | Descendant-held-pipe policy; migrate callers, then remove the unbounded form. |
| `ProcessSession` post-exit drain | no | optional (`ProcessPostExitDrain::WaitForEof` vs `AbandonAfter`) | Remove the unbounded default from ordinary product use. |
| `platform::process::DaemonChild::wait`, `SpawnedChild::wait` | yes | unbounded; bounded `wait_within` (polls `try_wait`, `TimedOut`, child left running) exists | Migrate callers, then remove the unbounded form. |
| `sync_spawn_group::SpawnedInner::wait` (Unix) | yes | unbounded, but the child lock is no longer held while blocked; callers bound it through `DaemonChild`/`SpawnedChild::wait_within`, which polls `try_wait` (#476) | **fixed** (#448): `kill` is no longer blocked by a concurrent `wait`. Remove the unbounded facade `wait` once callers migrate. |
| `platform_win::sync_spawn` `wait` | yes | unbounded; callers bound it through `DaemonChild`/`SpawnedChild::wait_within`, which polls `try_wait` (#476) | Remove the unbounded facade `wait` once callers migrate. |
| `SpawnHandle::wait(timeout, cancelled)` (`independent_spawn.rs`) | yes | bounded | none |

## File locks (`src/platform/fs.rs`)

| API | Blocks a native thread | Status | Required result |
| --- | --- | --- | --- |
| `lock_exclusive`, `lock_shared`, `lock_exclusive_owned`, `lock_shared_owned` | yes (`flock` / `LockFileEx`) | unbounded; bounded replacements `lock_{exclusive,shared}{,_owned}_for(file, timeout)` exist (`TimedOut` on expiry, owned forms keep the handle across retries) | Migrate zccache's direct callers (`staged_lock.rs`, `download_client/artifact/lock.rs`, `object_store.rs`, test helpers) and its `wait::lock_*_for` wrappers, then remove the unbounded forms. |
| `try_lock_exclusive`, `try_lock_shared` and the `_owned` variants | no | bounded (non-blocking) | none |

## Webview (`src/tauri.rs`)

| API | Blocks a native thread | Status | Required result |
| --- | --- | --- | --- |
| `WebviewHandle::wait_until_loaded`, `wait_until_terminal` | no | bounded (required `Duration`) | none |
| `WebviewHandle::wait_for_terminal` | no | unbounded; bounded `wait_until_terminal(timeout)` already exists | Migrate callers, or classify the unbounded form as an owner-lifetime listener. |
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
| `broker_client::connect_backend` | yes | partly bounded: broker Hello (internal 30 s default) and handoff (`handoff_ready_timeout`, 2 s) are bounded; the final backend connect, and the cached-endpoint attempt, are not | One caller-supplied deadline over the whole call. |
| `broker_client::BackendConnection` `Read` / `Write` | yes | unbounded; no timeout setter | Read and write budgets. |
| `broker_v2::connect` | yes | partly bounded: `deadline` covers negotiation only; the following `dial` -> `Stream::connect` is unbounded | Extend `deadline` over the dial. |
| `broker_v2::connect_published` | yes | unbounded (`Stream::connect`) | A connect budget. |
| `broker_v2::negotiate` | yes | bounded (`deadline`) | None. |
| `broker_v2::Connection` `Read` | yes | optional (`set_read_timeout`, default `None`) | Bound by default or require one. |
| `broker_v2::Connection` `Write` | yes | unbounded; no send timeout | A write budget. |
| `platform::ipc::Stream::connect` | yes | unbounded; on Windows the local-socket layer waits on a busy pipe without limit; bounded `Stream::connect_within(endpoint, timeout)` exists (`TimedOut`) | Use it in `broker_v2` and `broker_client`, then migrate callers. |
| `platform::ipc::Stream` `Read` | yes | optional (`set_recv_timeout`, default `None`) | Bound by default or require one. |
| `platform::ipc::Stream` `Write` / `flush` | yes | unbounded; the transport exposes only a receive timeout | A write budget. |
| `platform::ipc::Stream::transfer_to_backend` | yes | non-blocking on Linux (`MSG_DONTWAIT`); Windows not yet checked | Confirm on Windows. |
| `AsyncStream::connect`, `read`, `read_exact`, `write_all`, `flush`, `shutdown`, and the `AsyncReadHalf` / `AsyncWriteHalf` forms | no | unbounded; callers may wrap them in `async_engine::timeout` | `_within` forms, then migrate callers. |
| `LocalSocketStream::connect` (owner-only) | no | unbounded; callers own deadlines | A connect budget. |
| `LocalPipeClient::open` (Windows) | yes (one syscall) | bounded by design: pipe-busy is returned, not waited on | None. |
| `legacy_send_fd_to` | yes | connect unbounded (send is non-blocking) | Bound the connect or remove the deprecated function. |
| `legacy_send_fd_over`, `legacy_duplicate_handle` | yes (one syscall) | bounded by design | None. |
| `Listener::accept`, `InheritedListener` accept, `AsyncListener::accept`, `LocalSocketListener::accept`, `OwnerOnlyPipeInstance::connect` (Windows) | `Listener` yes, others no | unbounded by design; the async forms are cancellable and keep the listener usable | Classify as service-lifetime listeners. |

## Enforcement

None of these properties is enforced by a lint yet. The deny-level Dylints
required by #433 are still outstanding. `kernal_api_boundary` and
`kernal_api_platform_boundary` enforce placement only.
