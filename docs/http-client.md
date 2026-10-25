# HTTP client resource contract

The optional `http-client` capability owns GET, HEAD, POST, pull-based response
reads, and a blocking adapter driven by the caller's kernel runtime. Product
URLs, payload schemas, status interpretation, artifact publication and fallback
policy remain with the application. No backend types are public.

## TLS backend

Enable exactly one of the two features; both expose the same `http` module.

- `http-client` uses the platform's native TLS: OpenSSL on Linux,
  Security.framework on macOS, SChannel on Windows. Certificates are checked
  against the operating system's trust store.
- `http-client-rustls` uses rustls (ring provider) with the bundled Mozilla
  roots from `webpki-roots`, so the consumer links no OpenSSL and needs no
  system trust store (#427). rustls refuses a CA certificate presented as a
  leaf, so the native-TLS fixture tests run only under `http-client`.

Enabling both links both backends; reqwest then uses native TLS.

## Metadata and buffering

`Limits::max_header_bytes` and `max_header_count` are acceptance ceilings checked
after response parsing. They do not configure an allocator or bound process RSS.
The private HTTP/1 transport has independent parser limits; selecting larger
application limits does not relax these:

- Hyper 1.11.0 defaults to at most 100 response header fields, including repeated
  fields. A caller's larger count ceiling does not promise those fields succeed.
- Its incomplete response-head parsing threshold is `8192 + 4096 * 100` bytes
  (417792). Buffer capacity rounding and read-ahead mean this is not an exact
  allocation or complete-response-head byte limit.
- Chunk trailers and chunk extensions have separate 16-KiB parsing ceilings.
  Trailers are not exposed by the facade, but still have to be parsed safely.
- Reads retain at most one transport data chunk, plus transport/TLS buffers.
  The caller buffer bounds each returned transfer, not every private allocation.
  Body bytes are counted cumulatively before a received chunk is accepted.

Reqwest 0.12.28, Hyper 1.11.0 and Bytes 1.12.1 are exact optional private pins.
The Hyper constraint is intentional even though the facade does not name its
types: a library's lockfile alone does not constrain consumers' parser versions.
Reaudit `proto/h1/io.rs`, `role.rs`, and `decode.rs` when updating it. Real socket
regressions send excessive heads, fields, trailers and extensions with relaxed
application limits and require parser rejection, not a timeout. Those tests
verify rejection behavior, not an exact allocator/RSS measurement.

## Other safety defaults

Requests and response bodies have byte ceilings; URLs and application headers
are validated before copying. Connect, network-read and cumulative request/body
deadlines are finite. Dropping an operation or response releases its transport.
Automatic decompression is disabled even under downstream feature unification,
so content-encoded bytes and their headers are preserved.

Redirects default to zero and are capped at 32. Cross-origin hops discard all
application headers. POST becomes GET for 301/302/303; 307/308 replay is allowed
only within one origin. HTTPS downgrade is rejected before connecting. TLS uses
normal certificate and hostname verification. Local test-only trust fixtures
exercise successful TLS, untrusted certificates, wrong hostnames and downgrades;
they never modify the operating-system trust store.
