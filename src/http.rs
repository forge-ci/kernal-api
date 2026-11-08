//! Bounded HTTP client operations on the caller's async runtime.
//!
//! Redirects are opt-in; credentials are never inferred.
//! No proxy is ever used, including from the environment. URL hosts must be
//! ASCII as written: IDNA and percent-encoded hosts are rejected, not converted.
//! Response status interpretation and payload schemas belong to the caller.
//! Bodies preserve content-encoded wire bytes; decoding belongs to the caller.
//! HTTP/1 transport metadata has independent fixed parser ceilings. Caller
//! header limits are post-parse acceptance limits, not allocator/RSS ceilings.

use std::io;
use std::time::Duration;

// Local TLS fixtures for whichever backend is selected. rustls must refuse the
// self-signed CA used as its own leaf; both backends accept the CA-signed leaf.
#[cfg(test)]
#[path = "http_tls_tests.rs"]
mod tls_tests;

#[path = "http_url.rs"]
mod url;

use http_body_util::BodyExt;
use url::Target;

type HttpConnector = hyper_util::client::legacy::connect::HttpConnector;

// Native TLS wins when both features are enabled.
#[cfg(feature = "http-client")]
type Connector = hyper_tls::HttpsConnector<HttpConnector>;
#[cfg(all(feature = "http-client-rustls", not(feature = "http-client")))]
type Connector = hyper_rustls::HttpsConnector<HttpConnector>;

/// TCP only; the connector never consults proxy settings.
fn tcp_connector() -> HttpConnector {
    let mut http = HttpConnector::new();
    http.enforce_http(false);
    http.set_nodelay(true);
    http
}

/// Platform TLS verified against the operating system's trust store.
/// `extra_root` is a test-only PEM root added to one client.
#[cfg(feature = "http-client")]
fn connector(extra_root: Option<&[u8]>) -> io::Result<Connector> {
    let mut builder = native_tls::TlsConnector::builder();
    if let Some(pem) = extra_root {
        builder.add_root_certificate(
            native_tls::Certificate::from_pem(pem).map_err(io::Error::other)?,
        );
    }
    let tls = builder.build().map_err(io::Error::other)?;
    Ok(hyper_tls::HttpsConnector::from((
        tcp_connector(),
        tls.into(),
    )))
}

/// rustls with the ring provider and only the bundled webpki roots: no
/// OpenSSL, no platform verifier, no system trust store (#427).
/// `extra_root` is a test-only PEM root added to one client.
#[cfg(all(feature = "http-client-rustls", not(feature = "http-client")))]
fn connector(extra_root: Option<&[u8]>) -> io::Result<Connector> {
    use rustls::pki_types::pem::PemObject;
    let mut roots = rustls::RootCertStore {
        roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
    };
    if let Some(pem) = extra_root {
        for certificate in rustls::pki_types::CertificateDer::pem_slice_iter(pem) {
            roots
                .add(certificate.map_err(io::Error::other)?)
                .map_err(io::Error::other)?;
        }
    }
    let config = rustls::ClientConfig::builder_with_provider(std::sync::Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .map_err(io::Error::other)?
    .with_root_certificates(roots)
    .with_no_client_auth();
    Ok(hyper_rustls::HttpsConnectorBuilder::new()
        .with_tls_config(config)
        .https_or_http()
        .enable_http1()
        .wrap_connector(tcp_connector()))
}

/// Aborts the spawned HTTP/1 connection task, closing its socket, on drop.
struct Connection(tokio::task::JoinHandle<()>);

impl Drop for Connection {
    fn drop(&mut self) {
        self.0.abort();
    }
}

fn timed_out(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::TimedOut, message)
}

/// The smaller of `phase` and what remains of the total deadline.
fn phase_budget(phase: Duration, deadline: std::time::Instant) -> io::Result<Duration> {
    deadline
        .checked_duration_since(std::time::Instant::now())
        .filter(|remaining| !remaining.is_zero())
        .map(|remaining| remaining.min(phase))
        .ok_or_else(|| timed_out("HTTP total deadline expired"))
}

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
    connector: Connector,
    limits: Limits,
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

/// Transport failure. Backend errors never carry the request URL.
fn transport(error: impl Into<Box<dyn std::error::Error + Send + Sync>>) -> io::Error {
    io::Error::other(error)
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
    // The request future is bounded by the client's `Limits` (connect, total
    // and read timeouts), so the deprecated unbounded bridge cannot hang (#433).
    #[allow(deprecated)]
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
    // The request future is bounded by the client's `Limits` (connect, total
    // and read timeouts), so the deprecated unbounded bridge cannot hang (#433).
    #[allow(deprecated)]
    pub fn into_bytes(self) -> io::Result<Vec<u8>> {
        require_blocking_context()?;
        self.runtime.run(self.response.into_bytes())
    }
}

impl io::Read for BlockingResponse<'_> {
    // The request future is bounded by the client's `Limits` (connect, total
    // and read timeouts), so the deprecated unbounded bridge cannot hang (#433).
    #[allow(deprecated)]
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        require_blocking_context()?;
        self.runtime.run(self.response.read(buffer))
    }
}

impl Client {
    /// Build a transport with finite, nonzero timeouts and verified TLS.
    pub fn new(limits: Limits) -> io::Result<Self> {
        Self::with_connector(limits, connector(None)?)
    }

    fn with_connector(limits: Limits, connector: Connector) -> io::Result<Self> {
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
        Ok(Self { connector, limits })
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
            let previous = &response.target;
            let joined = previous.join(location);
            let next = Target::parse(&joined)?;
            if previous.is_https() && !next.is_https() {
                return Err(invalid("HTTP redirect would downgrade HTTPS"));
            }
            if !next.same_origin(previous) {
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
            url = std::borrow::Cow::Owned(joined);
        }
        Err(invalid("HTTP redirect limit exceeded"))
    }

    fn build_request(
        &self,
        request: &Request<'_>,
        target: &Target,
    ) -> io::Result<hyper::Request<http_body_util::Full<bytes::Bytes>>> {
        let method = match request.method {
            Method::Get => hyper::Method::GET,
            Method::Head => hyper::Method::HEAD,
            Method::Post => hyper::Method::POST,
        };
        let mut headers = hyper::HeaderMap::new();
        let mut remaining_request_headers = self.limits.max_header_bytes;
        for (name, value) in request.headers {
            remaining_request_headers = remaining_request_headers
                .checked_sub(name.len())
                .and_then(|remaining| remaining.checked_sub(value.len()))
                .ok_or_else(|| invalid("HTTP request headers exceed limit"))?;
            let name = hyper::header::HeaderName::from_bytes(name.as_bytes())
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
            let value = hyper::header::HeaderValue::from_str(value)
                .map_err(|_| invalid("invalid HTTP request header value"))?;
            headers.append(name, value);
        }
        headers
            .entry(hyper::header::ACCEPT)
            .or_insert(hyper::header::HeaderValue::from_static("*/*"));
        headers.insert(
            hyper::header::HOST,
            hyper::header::HeaderValue::from_str(&target.host_header())
                .map_err(|_| invalid("invalid HTTP URL"))?,
        );
        let body = if request.method == Method::Post {
            bytes::Bytes::copy_from_slice(request.body)
        } else {
            bytes::Bytes::new()
        };
        let mut built = hyper::Request::new(http_body_util::Full::new(body));
        *built.method_mut() = method;
        *built.uri_mut() = target
            .path_and_query()
            .parse()
            .map_err(|_| invalid("invalid HTTP URL"))?;
        *built.headers_mut() = headers;
        Ok(built)
    }

    /// Connect (TCP and TLS) and start a one-request HTTP/1 connection.
    async fn connect(
        &self,
        target: &Target,
        deadline: std::time::Instant,
    ) -> io::Result<(
        hyper::client::conn::http1::SendRequest<http_body_util::Full<bytes::Bytes>>,
        Connection,
    )> {
        let endpoint = target.endpoint()?;
        let mut connector = self.connector.clone();
        let connecting = async {
            std::future::poll_fn(|cx| tower_service::Service::poll_ready(&mut connector, cx))
                .await
                .map_err(transport)?;
            let stream = tower_service::Service::call(&mut connector, endpoint)
                .await
                .map_err(transport)?;
            hyper::client::conn::http1::Builder::new()
                .handshake(stream)
                .await
                .map_err(transport)
        };
        let budget = phase_budget(self.limits.connect_timeout, deadline)?;
        let (sender, connection) = tokio::time::timeout(budget, connecting)
            .await
            .map_err(|_| timed_out("HTTP connect timed out"))??;
        let connection = Connection(tokio::spawn(async move {
            // Request and body futures observe connection failures.
            let _ = connection.await;
        }));
        Ok((sender, connection))
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
        let target = Target::parse(request.url)?;
        let built = self.build_request(&request, &target)?;
        let (mut sender, connection) = self.connect(&target, deadline).await?;
        let budget = phase_budget(self.limits.read_timeout, deadline)?;
        let inner = tokio::time::timeout(budget, sender.send_request(built))
            .await
            .map_err(|_| timed_out("HTTP response headers timed out"))?
            .map_err(transport)?;
        let (parts, body) = inner.into_parts();
        if parts.headers.len() > self.limits.max_header_count {
            return Err(invalid("HTTP response header count exceeds limit"));
        }
        let mut remaining_headers = self.limits.max_header_bytes;
        for (name, value) in &parts.headers {
            remaining_headers = remaining_headers
                .checked_sub(name.as_str().len())
                .and_then(|remaining| remaining.checked_sub(value.as_bytes().len()))
                .ok_or_else(|| invalid("HTTP response headers exceed limit"))?;
        }
        if request.method != Method::Head
            && hyper::body::Body::size_hint(&body)
                .exact()
                .is_some_and(|size| size > self.limits.max_body_bytes)
        {
            return Err(invalid("HTTP response body exceeds limit"));
        }
        Ok(Response {
            deadline,
            read_timeout: self.limits.read_timeout,
            status: parts.status.as_u16(),
            headers: parts.headers,
            body,
            target,
            _connection: connection,
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
    read_timeout: Duration,
    status: u16,
    headers: hyper::HeaderMap,
    body: hyper::body::Incoming,
    target: Target,
    _connection: Connection,
    remaining: u64,
    pending: bytes::Bytes,
    failed: bool,
    eof: bool,
}

impl Response {
    /// First response header value, looked up case-insensitively.
    /// Returned bytes are valid only while this response is borrowed.
    pub fn header(&self, name: &str) -> Option<&[u8]> {
        self.headers.get(name).map(|value| value.as_bytes())
    }

    /// Numeric HTTP status. Non-success status is not a transport error.
    pub fn status(&self) -> u16 {
        self.status
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
            return Err(timed_out("HTTP total deadline expired"));
        }
        while self.pending.is_empty() && !self.eof {
            if let Err(error) = self.pull().await {
                self.failed = true;
                return Err(error);
            }
        }
        let n = buffer.len().min(self.pending.len());
        buffer[..n].copy_from_slice(&self.pending.split_to(n));
        Ok(n)
    }

    /// Wait for the next body frame under the read and total deadlines.
    /// Trailers are parsed by the transport but not exposed.
    async fn pull(&mut self) -> io::Result<()> {
        let budget = phase_budget(self.read_timeout, self.deadline)?;
        match tokio::time::timeout(budget, self.body.frame()).await {
            Err(_) => Err(timed_out("HTTP response read timed out")),
            Ok(None) => {
                self.eof = true;
                Ok(())
            }
            Ok(Some(Err(error))) => Err(transport(error)),
            Ok(Some(Ok(frame))) => {
                if let Ok(chunk) = frame.into_data() {
                    self.remaining = self
                        .remaining
                        .checked_sub(chunk.len() as u64)
                        .ok_or_else(|| invalid("HTTP response body exceeds limit"))?;
                    self.pending = chunk;
                }
                Ok(())
            }
        }
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
