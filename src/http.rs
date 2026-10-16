//! Bounded HTTP client operations on the caller's async runtime.
//!
//! Redirects are opt-in; credentials are never inferred.
//! Response status interpretation and payload schemas belong to the caller.
//! Bodies preserve content-encoded wire bytes; decoding belongs to the caller.
//! HTTP/1 transport metadata has independent fixed parser ceilings. Caller
//! header limits are post-parse acceptance limits, not allocator/RSS ceilings.

use std::io;
use std::time::Duration;

// Native TLS fixtures: the self-signed identity is a CA used as its own leaf,
// which rustls (`http-client-rustls` alone) rightly refuses.
#[cfg(all(test, feature = "http-client"))]
#[path = "http_tls_tests.rs"]
mod tls_tests;

/// Per-request ceilings. A client can be reused for requests with this policy.
/// All timeouts must be nonzero and at most 365 days.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    /// Redirect hops allowed (at most 32). Zero returns 3xx without following.
    /// Cross-origin hops discard all application headers; HTTPS downgrade is
    /// rejected. POST becomes GET for 301/302/303. For 307/308, POST is replayed
    /// only within the same origin; cross-origin POST replay is rejected.
    pub max_redirects: u8,
    /// Maximum buffered request body.
    pub max_request_bytes: usize,
    /// Maximum encoded URL length before parsing.
    pub max_url_bytes: usize,
    /// Maximum response body accepted, including streaming reads.
    pub max_body_bytes: u64,
    /// Maximum accepted response header names and values in aggregate.
    /// Checked after the backend has parsed headers, not before allocation.
    pub max_header_bytes: usize,
    /// Maximum request or response header values, including duplicates.
    /// At most 1024, to stay below the private transport's header-map ceiling.
    /// The HTTP/1 response parser independently rejects more than 100 fields.
    pub max_header_count: usize,
    /// Maximum connection-establishment duration.
    pub connect_timeout: Duration,
    /// Maximum time from request start through response-body completion.
    pub total_timeout: Duration,
    /// Maximum wait for the next network read, reset on read progress.
    pub read_timeout: Duration,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_redirects: 0,
            max_request_bytes: 4 * 1024 * 1024,
            max_url_bytes: 8192,
            max_body_bytes: 4 * 1024 * 1024,
            max_header_bytes: 64 * 1024,
            max_header_count: 128,
            connect_timeout: Duration::from_secs(10),
            total_timeout: Duration::from_secs(120),
            read_timeout: Duration::from_secs(30),
        }
    }
}

/// Supported request operations.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Method {
    /// Retrieve a representation.
    Get,
    /// Retrieve representation metadata without a response body.
    Head,
    /// Submit an application payload.
    Post,
}

/// Borrowed application request. Body and headers are validated before copying.
pub struct Request<'a> {
    /// HTTP operation.
    pub method: Method,
    /// Absolute HTTP(S) URL without embedded credentials.
    pub url: &'a str,
    /// Application headers; connection/framing headers are transport-owned.
    pub headers: &'a [(&'a str, &'a str)],
    /// Small POST payload. GET and HEAD require an empty body.
    pub body: &'a [u8],
}

impl<'a> Request<'a> {
    /// Construct a GET with no extra headers or body.
    pub fn get(url: &'a str) -> Self {
        Self {
            method: Method::Get,
            url,
            headers: &[],
            body: &[],
        }
    }
}

/// Reusable HTTP transport; does not construct an executor or runtime.
#[derive(Clone)]
pub struct Client {
    inner: reqwest::Client,
    limits: Limits,
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn transport(error: reqwest::Error) -> io::Error {
    let kind = if error.is_timeout() {
        io::ErrorKind::TimedOut
    } else {
        io::ErrorKind::Other
    };
    // URLs can contain application secrets; diagnostics must not echo them.
    io::Error::new(kind, error.without_url())
}

fn require_blocking_context() -> io::Result<()> {
    if crate::async_engine::RuntimeHandle::current().is_ok() {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "blocking HTTP cannot run inside an async runtime; use the async client",
        ));
    }
    Ok(())
}

/// Blocking view of the same HTTP transport, driven by a caller-owned runtime.
/// Does not create a runtime or a background worker. Calls from an entered
/// async runtime return `WouldBlock`, rather than attempting nested execution.
pub struct BlockingClient<'runtime> {
    client: Client,
    runtime: &'runtime crate::async_engine::Runtime,
}

impl<'runtime> BlockingClient<'runtime> {
    /// Bind the client to an existing kernel runtime.
    pub fn new(
        runtime: &'runtime crate::async_engine::Runtime,
        limits: Limits,
    ) -> io::Result<Self> {
        require_blocking_context()?;
        Ok(Self {
            client: Client::new(limits)?,
            runtime,
        })
    }

    /// Issue a GET without collecting its body.
    pub fn get(&self, url: &str) -> io::Result<BlockingResponse<'runtime>> {
        self.execute(Request::get(url))
    }

    /// Execute on the supplied runtime; all async request limits also apply.
    pub fn execute(&self, request: Request<'_>) -> io::Result<BlockingResponse<'runtime>> {
        require_blocking_context()?;
        Ok(BlockingResponse {
            response: self.runtime.run(self.client.execute(request))?,
            runtime: self.runtime,
        })
    }
}

/// Streaming blocking response tied to its caller-owned runtime.
/// Implements `Read` without collecting the full body. Drop releases the
/// response; the borrowed runtime remains owned by the caller.
pub struct BlockingResponse<'runtime> {
    response: Response,
    runtime: &'runtime crate::async_engine::Runtime,
}

impl BlockingResponse<'_> {
    /// Numeric status; non-success statuses are not transport errors.
    pub fn status(&self) -> u16 {
        self.response.status()
    }

    /// First header value, looked up case-insensitively.
    pub fn header(&self, name: &str) -> Option<&[u8]> {
        self.response.header(name)
    }

    /// Collect only unread bytes, under the same cumulative body limit.
    pub fn into_bytes(self) -> io::Result<Vec<u8>> {
        require_blocking_context()?;
        self.runtime.run(self.response.into_bytes())
    }
}

impl io::Read for BlockingResponse<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        require_blocking_context()?;
        self.runtime.run(self.response.read(buffer))
    }
}

impl Client {
    /// Build a transport with finite, nonzero timeouts and verified TLS.
    pub fn new(limits: Limits) -> io::Result<Self> {
        Self::with_builder(limits, reqwest::Client::builder())
    }

    fn with_builder(limits: Limits, builder: reqwest::ClientBuilder) -> io::Result<Self> {
        if limits.max_redirects > 32 {
            return Err(invalid("HTTP redirect limit cannot exceed 32"));
        }
        if limits.max_header_count > 1024 {
            return Err(invalid("HTTP header count limit cannot exceed 1024"));
        }
        for timeout in [
            limits.connect_timeout,
            limits.total_timeout,
            limits.read_timeout,
        ] {
            if timeout.is_zero()
                || timeout > Duration::from_secs(365 * 24 * 60 * 60)
                || std::time::Instant::now().checked_add(timeout).is_none()
            {
                return Err(invalid(
                    "HTTP timeouts must be nonzero and at most 365 days",
                ));
            }
        }
        let inner = builder
            // Cargo unifies backend features across the consumer's graph.
            // Preserve wire bytes even if another user enables auto-decoding.
            .no_gzip()
            .no_brotli()
            .no_deflate()
            .no_zstd()
            .connect_timeout(limits.connect_timeout)
            .timeout(limits.total_timeout)
            .read_timeout(limits.read_timeout)
            .redirect(reqwest::redirect::Policy::none())
            .http1_only()
            .pool_max_idle_per_host(0)
            .build()
            .map_err(transport)?;
        Ok(Self { inner, limits })
    }

    /// Issue a GET, following redirects only when explicitly enabled in limits.
    /// Drop the future or response to release the in-flight operation.
    pub async fn get(&self, url: &str) -> io::Result<Response> {
        self.execute(Request::get(url)).await
    }

    /// Send a validated request. Status interpretation belongs to the caller.
    /// Request and response headers each have an independent metadata ceiling.
    pub async fn execute(&self, request: Request<'_>) -> io::Result<Response> {
        let deadline = std::time::Instant::now()
            .checked_add(self.limits.total_timeout)
            .ok_or_else(|| invalid("HTTP deadline cannot be represented"))?;
        let mut url = std::borrow::Cow::Borrowed(request.url);
        let mut headers = std::borrow::Cow::Borrowed(request.headers);
        let mut method = request.method;
        let mut body = request.body;
        for hop in 0..=self.limits.max_redirects {
            let response = self
                .execute_once(
                    Request {
                        method,
                        url: &url,
                        headers: &headers,
                        body,
                    },
                    deadline,
                )
                .await?;
            if self.limits.max_redirects == 0 || !matches!(response.status(), 301..=303 | 307..=308)
            {
                return Ok(response);
            }
            let Some(location) = response.header("location") else {
                return Ok(response);
            };
            if hop == self.limits.max_redirects {
                return Err(invalid("HTTP redirect limit exceeded"));
            }
            if location.len() > self.limits.max_url_bytes {
                return Err(invalid("HTTP redirect URL exceeds limit"));
            }
            let location =
                std::str::from_utf8(location).map_err(|_| invalid("invalid HTTP redirect URL"))?;
            let previous = response.inner.url();
            let next = previous
                .join(location)
                .map_err(|_| invalid("invalid HTTP redirect URL"))?;
            if previous.scheme() == "https" && next.scheme() != "https" {
                return Err(invalid("HTTP redirect would downgrade HTTPS"));
            }
            if next.origin() != previous.origin() {
                // A POST payload can contain credentials even when every
                // application header has been discarded. Following redirects
                // does not grant a second origin authority to receive it.
                if method == Method::Post && matches!(response.status(), 307 | 308) {
                    return Err(invalid("HTTP redirect would replay POST across origins"));
                }
                headers = std::borrow::Cow::Borrowed(&[]);
            }
            if method == Method::Post && matches!(response.status(), 301..=303) {
                method = Method::Get;
                body = &[];
                headers.to_mut().retain(|(name, _)| {
                    !name.eq_ignore_ascii_case("content-type")
                        && !name.eq_ignore_ascii_case("content-encoding")
                });
            }
            // Drop the intermediate body before the next hop. Every response
            // has already passed the same header/body-declaration limits.
            url = std::borrow::Cow::Owned(next.into());
        }
        Err(invalid("HTTP redirect limit exceeded"))
    }

    async fn execute_once(
        &self,
        request: Request<'_>,
        deadline: std::time::Instant,
    ) -> io::Result<Response> {
        if request.url.len() > self.limits.max_url_bytes
            || request.body.len() > self.limits.max_request_bytes
            || request.headers.len() > self.limits.max_header_count
        {
            return Err(invalid("HTTP request exceeds limit"));
        }
        if request.method != Method::Post && !request.body.is_empty() {
            return Err(invalid("only POST supports a request body"));
        }
        let url = reqwest::Url::parse(request.url).map_err(|_| invalid("invalid HTTP URL"))?;
        if !matches!(url.scheme(), "http" | "https")
            || !url.username().is_empty()
            || url.password().is_some()
        {
            return Err(invalid("HTTP URL scheme or embedded credentials rejected"));
        }
        let method = match request.method {
            Method::Get => reqwest::Method::GET,
            Method::Head => reqwest::Method::HEAD,
            Method::Post => reqwest::Method::POST,
        };
        let mut builder = self.inner.request(method, url);
        let mut remaining_request_headers = self.limits.max_header_bytes;
        for (name, value) in request.headers {
            remaining_request_headers = remaining_request_headers
                .checked_sub(name.len())
                .and_then(|remaining| remaining.checked_sub(value.len()))
                .ok_or_else(|| invalid("HTTP request headers exceed limit"))?;
            let name = reqwest::header::HeaderName::from_bytes(name.as_bytes())
                .map_err(|_| invalid("invalid HTTP request header name"))?;
            if matches!(
                name.as_str(),
                "host"
                    | "content-length"
                    | "transfer-encoding"
                    | "connection"
                    | "upgrade"
                    | "trailer"
                    | "te"
                    | "expect"
                    | "proxy-authorization"
            ) {
                return Err(invalid(
                    "HTTP framing and proxy headers are transport-owned",
                ));
            }
            let value = reqwest::header::HeaderValue::from_str(value)
                .map_err(|_| invalid("invalid HTTP request header value"))?;
            builder = builder.header(name, value);
        }
        if request.method == Method::Post {
            builder = builder.body(request.body.to_vec());
        }
        let timeout = deadline
            .checked_duration_since(std::time::Instant::now())
            .filter(|duration| !duration.is_zero())
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::TimedOut, "HTTP total deadline expired")
            })?;
        let inner = builder.timeout(timeout).send().await.map_err(transport)?;
        if inner.headers().len() > self.limits.max_header_count {
            return Err(invalid("HTTP response header count exceeds limit"));
        }
        let mut remaining_headers = self.limits.max_header_bytes;
        for (name, value) in inner.headers() {
            remaining_headers = remaining_headers
                .checked_sub(name.as_str().len())
                .and_then(|remaining| remaining.checked_sub(value.as_bytes().len()))
                .ok_or_else(|| invalid("HTTP response headers exceed limit"))?;
        }
        if request.method != Method::Head
            && inner
                .content_length()
                .is_some_and(|size| size > self.limits.max_body_bytes)
        {
            return Err(invalid("HTTP response body exceeds limit"));
        }
        Ok(Response {
            deadline,
            inner,
            remaining: self.limits.max_body_bytes,
            pending: bytes::Bytes::new(),
            failed: false,
            eof: false,
        })
    }
}

/// Owned response, with backend resources released when dropped.
pub struct Response {
    deadline: std::time::Instant,
    inner: reqwest::Response,
    remaining: u64,
    pending: bytes::Bytes,
    failed: bool,
    eof: bool,
}

impl Response {
    /// First response header value, looked up case-insensitively.
    /// Returned bytes are valid only while this response is borrowed.
    pub fn header(&self, name: &str) -> Option<&[u8]> {
        self.inner.headers().get(name).map(|value| value.as_bytes())
    }

    /// Numeric HTTP status. Non-success status is not a transport error.
    pub fn status(&self) -> u16 {
        self.inner.status().as_u16()
    }

    /// Read at most the caller's buffer length, pulling only when needed.
    /// An empty buffer returns zero without reading; otherwise zero means EOF.
    /// The total body ceiling applies across all reads. Errors are terminal;
    /// drop the response to release the transport after an error.
    /// Retains at most one private transport chunk, not the complete body.
    pub async fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if self.failed {
            return Err(invalid("HTTP response body is in a failed state"));
        }
        if buffer.is_empty() {
            return Ok(0);
        }
        if !self.eof && std::time::Instant::now() >= self.deadline {
            self.failed = true;
            self.pending = bytes::Bytes::new();
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "HTTP total deadline expired",
            ));
        }
        while self.pending.is_empty() && !self.eof {
            match self.inner.chunk().await {
                Ok(Some(chunk)) => {
                    let Some(remaining) = self.remaining.checked_sub(chunk.len() as u64) else {
                        self.failed = true;
                        return Err(invalid("HTTP response body exceeds limit"));
                    };
                    self.remaining = remaining;
                    self.pending = chunk;
                }
                Ok(None) => self.eof = true,
                Err(error) => {
                    self.failed = true;
                    return Err(transport(error));
                }
            }
        }
        let n = buffer.len().min(self.pending.len());
        buffer[..n].copy_from_slice(&self.pending.split_to(n));
        Ok(n)
    }

    /// Consume the unread part of a small response under its byte ceiling.
    /// No declared length is trusted for allocation or stream completion.
    pub async fn into_bytes(mut self) -> io::Result<Vec<u8>> {
        let mut body = Vec::new();
        let mut buffer = [0; 64 * 1024];
        loop {
            let n = self.read(&mut buffer).await?;
            if n == 0 {
                break;
            }
            body.try_reserve_exact(n).map_err(io::Error::other)?;
            body.extend_from_slice(&buffer[..n]);
        }
        Ok(body)
    }
}
