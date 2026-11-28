# Secure entropy

Enable `secure-random` for `random::SecureRandom`. It uses the existing private
OS entropy backend, independently of crash, profiling, GUI and WASM facilities.
This does not claim to remove every transitive random dependency from the
default host graph.

Reuse one instance across requests. Configure 1–64 outstanding operations and
a positive caller timeout of at most one day. Each request accepts 0–65536
bytes. Oversized requests fail before admission or allocation; saturation fails
immediately without a waiter queue. Empty requests need no runtime or OS call.
Nonempty requests require the kernel runtime with timers enabled.

Entropy initialization can block inside the OS. Timeout and cancellation bound
the caller's wait, not native execution. A native worker retains its permit
until it ends, and cancelled work still waiting to start skips the OS call.
No fallback is used and no partial output is returned on failure. Errors contain
semantic categories only, not bytes or backend error types. Returned `Vec<u8>`
storage is ordinary memory, not a protected or zeroizing secret container.

`SecureRandom::fill(&mut [u8])` (#631) is the synchronous form for callers
without a runtime. It takes no instance, budget, timeout or worker: it calls the
OS CSPRNG on the caller's thread, may block until the OS entropy pool is
initialized, and has no length limit because the caller owns the buffer. Empty
buffers skip the OS call; on `Unavailable` the buffer contents are unspecified.

Applications choose token length, formatting, storage and authorization policy.
Generic failure, admission and cancellation tests belong here. Native smoke
tests verify availability and result shape; they are not statistical proof of
cryptographic quality. First consumer: FastLED's 32-byte test capability token,
tracked in issue #180. Release and consumer adoption remain pending.
