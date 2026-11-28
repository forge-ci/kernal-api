//! Bounded native HTTP/1.1 and HTTP/2 serving on the caller's runtime.
//!
//! Applications own routing, authorization and response policy. Dropping the
//! serving future closes the listener and cancels all owned connections.

use bytes::Bytes;
use http_body_util::{BodyExt, Limited};
use hyper::{body::Incoming, service::service_fn};
use hyper_util::{
    rt::{TokioIo, TokioTimer},
    server::conn::auto,
};
#[cfg(feature = "websocket")]
use std::pin::Pin;
use std::{convert::Infallible, future::Future, io, net::SocketAddr, sync::Arc, time::Duration};
use tokio::io::{AsyncRead, AsyncWrite};

#[cfg(feature = "websocket")]
mod websocket;
#[cfg(feature = "websocket")]
pub use websocket::{
    Message as WebSocketMessage, Upgrade as WebSocketUpgrade, WebSocket, WebSocketLimits,
};

mod body;
mod diagnostics;
#[cfg(all(test, any(feature = "http-client", feature = "http-client-rustls")))]
mod h2_tests;
mod request_body;
mod target;
mod transport;
use body::ServerBody;
use diagnostics::increment;
pub use diagnostics::{Diagnostics, Snapshot};
pub use target::{QueryPairs, RouteParams};
pub use transport::ConnectionStream;

/// One server-sent event. `id` is the reconnect cursor and `event` is its type.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SseEvent {
    pub id: Option<String>,
    pub event: Option<String>,
    pub data: String,
}

#[cfg(feature = "websocket")]
type UpgradeTask = Pin<Box<dyn Future<Output = ()> + Send + 'static>>;

/// Shared bounded native preparation of file responses. Clones share admission;
/// use one instance for a server's routes, not one instance per request.
#[derive(Clone, Debug)]
pub struct FileResponses {
    budget: body::ReadBudget,
    timeout: Duration,
}

impl FileResponses {
    /// Configure 1..=64 outstanding preparations and a positive deadline up to
    /// one day. No runtime is constructed.
    ///
    /// # Errors
    /// Invalid limits return `InvalidInput`.
    pub fn new(max_operations: usize, timeout: Duration) -> io::Result<Self> {
        if !(1..=64).contains(&max_operations)
            || timeout.is_zero()
            || timeout > Duration::from_secs(86400)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid file preparation limits",
            ));
        }
        Ok(Self {
            budget: body::ReadBudget::new(max_operations),
            timeout,
        })
    }

    /// Inspect and open a caller-authorized regular file, preparing its streamed
    /// response off the async executor. Prefixes are limited to 1 MiB. Paths may
    /// follow symlinks; callers still own authorization and directory policy.
    /// Cancellation/deadlines cannot interrupt an in-flight native call: its
    /// permit remains held until completion. Saturation fails without queuing.
    ///
    /// # Errors
    /// Reports native failures, non-files/oversize prefixes (`InvalidInput`),
    /// capacity exhaustion (`WouldBlock`), deadlines (`TimedOut`), and missing
    /// runtime context. Requires timers enabled on the caller's runtime.
    pub async fn open(&self, path: std::path::PathBuf, prefix: Vec<u8>) -> io::Result<Response> {
        crate::async_engine::RuntimeHandle::current().map_err(io::Error::other)?;
        if prefix.len() > 1024 * 1024 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "file prefix exceeds limit",
            ));
        }
        let task = self.budget.spawn(move || {
            // Avoid opening known FIFOs/devices. A concurrent path replacement
            // can still block open, so worker-held admission remains essential.
            if !std::fs::metadata(&path)?.is_file() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "response path is not a regular file",
                ));
            }
            Response::file(std::fs::File::open(path)?, prefix)
        })?;
        tokio::time::timeout(self.timeout, task)
            .await
            .map_err(|_| {
                io::Error::new(io::ErrorKind::TimedOut, "file preparation deadline expired")
            })?
            .map_err(io::Error::other)?
    }
}

/// Per-server resource and time limits. Body limits bound accepted values, not
/// memory already allocated by application handlers before returning a response.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    /// Maximum concurrently owned connections. The accept loop waits at capacity.
    /// Also bounds outstanding native file-read workers in a separate shared
    /// budget. A cancelled connection's native read retains its read slot until
    /// the OS call finishes; new file reads fail when this budget is exhausted.
    pub max_connections: usize,
    /// Maximum request body bytes, whether buffered or streamed.
    pub max_request_body_bytes: usize,
    /// Maximum accepted in-memory response body bytes or streamed source item bytes.
    pub max_response_body_bytes: usize,
    /// Maximum file bytes (including an optional prefix), streamed rather than collected.
    pub max_file_bytes: u64,
    /// Maximum response frame size, from 1024 through 65536 bytes.
    pub max_stream_chunk_bytes: usize,
    /// Maximum UTF-8 SSE payload bytes before encoding (encoding adds bounded overhead).
    pub max_event_bytes: usize,
    /// HTTP/1 parser read-buffer and HTTP/2 decoded header list bound (at least 8192 bytes).
    pub max_header_bytes: usize,
    /// Maximum parsed header count.
    pub max_headers: usize,
    /// Maximum accepted application response header entries, counting duplicates.
    pub max_response_headers: usize,
    /// Maximum application header bytes: name + value + four framing bytes per
    /// entry. Excludes transport-generated headers and the status line; this is
    /// an acceptance limit, not a limit on prior application allocations.
    pub max_response_header_bytes: usize,
    /// Maximum time for protocol detection and each HTTP/1 request header.
    pub header_timeout: Duration,
    /// Maximum elapsed time to receive a request body.
    pub body_timeout: Duration,
    /// Maximum time for application response preparation.
    pub handler_timeout: Duration,
    /// Maximum time an attempted socket write/flush may remain without progress.
    pub write_timeout: Duration,
    /// Absolute lifetime of a connection, including response transmission.
    pub connection_timeout: Duration,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_connections: 64,
            max_request_body_bytes: 2 * 1024 * 1024,
            max_response_body_bytes: 64 * 1024 * 1024,
            max_file_bytes: 16 * 1024 * 1024 * 1024,
            max_stream_chunk_bytes: 64 * 1024,
            max_event_bytes: 64 * 1024,
            max_header_bytes: 32 * 1024,
            max_headers: 100,
            max_response_headers: 100,
            max_response_header_bytes: 32 * 1024,
            header_timeout: Duration::from_secs(10),
            body_timeout: Duration::from_secs(30),
            handler_timeout: Duration::from_secs(30),
            write_timeout: Duration::from_secs(30),
            connection_timeout: Duration::from_secs(3600),
        }
    }
}

impl Limits {
    fn validate(self) -> io::Result<()> {
        if self.max_connections == 0
            || self.max_connections > 65_536
            || !(8192..=1024 * 1024).contains(&self.max_header_bytes)
            || !(1..=1024).contains(&self.max_headers)
            || self.max_response_headers > 1024
            || self.max_response_header_bytes > 1024 * 1024
            || self.max_request_body_bytes > 1024 * 1024 * 1024
            || self.max_response_body_bytes > 1024 * 1024 * 1024
            || self.max_file_bytes > 1024 * 1024 * 1024 * 1024
            || !(1024..=65536).contains(&self.max_stream_chunk_bytes)
            || !(1..=1024 * 1024).contains(&self.max_event_bytes)
            || [
                self.header_timeout,
                self.body_timeout,
                self.handler_timeout,
                self.write_timeout,
                self.connection_timeout,
            ]
            .into_iter()
            .any(|d| d.is_zero() || d > Duration::from_secs(365 * 24 * 3600))
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid HTTP server limits",
            ));
        }
        Ok(())
    }
}

/// An owned request with a bounded buffered or streaming body.
#[derive(Debug)]
pub struct Request {
    method: String,
    #[cfg(feature = "websocket")]
    http_1_1: bool,
    target: String,
    uri: hyper::Uri,
    headers: hyper::HeaderMap,
    body: Vec<u8>,
    streaming_body: Option<request_body::IncomingBody>,
    #[cfg(feature = "websocket")]
    upgrade: Option<hyper::upgrade::OnUpgrade>,
}

impl Request {
    /// HTTP method token.
    pub fn method(&self) -> &str {
        &self.method
    }
    /// Original request target including an optional query string.
    pub fn target(&self) -> &str {
        &self.target
    }
    /// Encoded URI path, without the query. No percent decoding or normalization.
    pub fn path(&self) -> &str {
        self.uri.path()
    }
    /// Decode the path once as UTF-8, preserving literal `+`. This does not
    /// authorize filesystem access or normalize dots, slashes, backslashes or NUL.
    ///
    /// # Errors
    /// Returns `InvalidData` for malformed escapes or non-UTF-8 bytes.
    pub fn decoded_path(&self) -> io::Result<String> {
        target::decode(self.path(), false)
    }
    /// Encoded query without `?`; absent and present-but-empty remain distinct.
    pub fn query(&self) -> Option<&str> {
        self.uri.query()
    }
    /// Iterate decoded form-query pairs without collecting them. Input size is
    /// bounded by request parsing; each decoded field is no larger than its
    /// encoded bytes. Applications own duplicate and unknown-parameter policy.
    pub fn query_pairs(&self) -> QueryPairs<'_> {
        QueryPairs::new(self.query().unwrap_or(""))
    }
    /// First matching header's raw bytes; names are case-insensitive.
    pub fn header(&self, name: &str) -> Option<&[u8]> {
        self.headers.get(name).map(|value| value.as_bytes())
    }
    /// Accepted body bytes. This slice is empty in streaming mode.
    pub fn body(&self) -> &[u8] {
        &self.body
    }

    /// Match the method and encoded path, capturing each `:name` as one
    /// nonempty segment. Capture values retain their original percent escapes.
    /// The query is ignored and trailing slashes must match exactly.
    pub fn match_route<'a>(&'a self, method: &str, pattern: &'a str) -> Option<RouteParams<'a>> {
        if self.method != method {
            return None;
        }
        RouteParams::matches(self.path(), pattern)
    }

    /// Read the next data chunk in streaming mode. Returns `None` after the
    /// body ends or when this request was dispatched with a buffered body.
    ///
    /// # Errors
    /// Returns `FileTooLarge` when the total byte limit is exceeded,
    /// `TimedOut` when the body deadline elapses, or `InvalidData` when
    /// the peer's body fails. The error persists on subsequent reads.
    pub async fn next_chunk(&mut self) -> io::Result<Option<Vec<u8>>> {
        match &mut self.streaming_body {
            Some(body) => body.next_chunk().await,
            None => Ok(None),
        }
    }

    /// Consume this request as an RFC 6455 WebSocket upgrade. Validate route,
    /// origin, host and authorization before calling this method. The returned
    /// value owns the upgrade future; it cannot be reused as an HTTP request.
    #[cfg(feature = "websocket")]
    pub fn into_websocket(self) -> io::Result<WebSocketUpgrade> {
        websocket::Upgrade::from_request(self)
    }
}

/// An application-selected HTTP response with validated status and headers.
pub struct Response {
    status: hyper::StatusCode,
    headers: hyper::HeaderMap,
    body: ServerBody,
    #[cfg(feature = "websocket")]
    upgrade_task: Option<UpgradeTask>,
}

impl std::fmt::Debug for Response {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Response")
            .field("status", &self.status)
            .field("headers", &self.headers)
            .field("body", &self.body)
            .finish_non_exhaustive()
    }
}

impl Default for Response {
    /// An empty 500 response for fallible application response preparation.
    fn default() -> Self {
        Self {
            status: hyper::StatusCode::INTERNAL_SERVER_ERROR,
            headers: hyper::HeaderMap::new(),
            body: ServerBody::bytes(Bytes::new()),
            #[cfg(feature = "websocket")]
            upgrade_task: None,
        }
    }
}

impl Response {
    #[cfg(feature = "websocket")]
    pub(super) fn websocket_upgrade(accept: &str, upgrade_task: UpgradeTask) -> Self {
        let mut headers = hyper::HeaderMap::new();
        headers.insert(
            hyper::header::CONNECTION,
            hyper::header::HeaderValue::from_static("Upgrade"),
        );
        headers.insert(
            hyper::header::UPGRADE,
            hyper::header::HeaderValue::from_static("websocket"),
        );
        headers.insert(
            hyper::header::HeaderName::from_static("sec-websocket-accept"),
            hyper::header::HeaderValue::from_str(accept)
                .expect("derived WebSocket accept key is valid"),
        );
        Self {
            status: hyper::StatusCode::SWITCHING_PROTOCOLS,
            headers,
            body: ServerBody::bytes(Bytes::new()),
            upgrade_task: Some(upgrade_task),
        }
    }

    /// Construct a response. The server separately enforces its body-size limit.
    ///
    /// # Errors
    /// Rejects status values outside 200..=599; interim responses are transport-owned.
    /// Statuses 204, 205 and 304 require an empty body.
    pub fn new(status: u16, body: impl Into<Vec<u8>>) -> io::Result<Self> {
        if !(200..=599).contains(&status) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid final HTTP status",
            ));
        }
        let body = body.into();
        if matches!(status, 204 | 205 | 304) && !body.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "HTTP status does not permit response content",
            ));
        }
        Ok(Self {
            status: hyper::StatusCode::from_u16(status).map_err(io::Error::other)?,
            headers: hyper::HeaderMap::new(),
            body: ServerBody::bytes(Bytes::from(body)),
            #[cfg(feature = "websocket")]
            upgrade_task: None,
        })
    }

    /// Stream an already-opened regular file from its current position to its
    /// current length, optionally preceded by `prefix`. The caller owns file
    /// selection/authorization. Later growth is ignored; truncation is an error.
    ///
    /// Metadata and position inspection are synchronous native operations. File
    /// reads are asynchronous and bounded; cancelling does not forcibly interrupt
    /// an OS read already running in the runtime's blocking I/O pool.
    ///
    /// # Errors
    /// Rejects non-regular files, prefixes larger than 1 MiB, and native metadata
    /// or position failures. Server acceptance also applies its configured limits.
    pub fn file(file: std::fs::File, prefix: impl Into<Vec<u8>>) -> io::Result<Self> {
        let mut response = Self::new(200, Vec::new())?;
        response.body = ServerBody::file(file, prefix.into())?;
        Ok(response)
    }

    /// Stream owned chunks on demand. The server limits each source item
    /// using `max_response_body_bytes` and splits it into bounded frames.
    /// Dropping the response drops its source.
    ///
    /// # Errors
    /// Rejects invalid final statuses and statuses that disallow content.
    /// Source errors and oversized items terminate the response stream.
    pub fn stream<S>(status: u16, chunks: S) -> io::Result<Self>
    where
        S: futures_core::Stream<Item = io::Result<Vec<u8>>> + Send + 'static,
    {
        let mut response = Self::new(status, Vec::new())?;
        if matches!(status, 204 | 205 | 304) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "HTTP status does not permit response content",
            ));
        }
        response.body = ServerBody::stream(chunks);
        Ok(response)
    }

    /// Encode a pull-driven sequence of SSE data events. Keepalives are comments;
    /// the next event is polled only when the transport requests another frame.
    /// Dropping the response drops its source. No producer task or queue is added.
    ///
    /// # Errors
    /// Rejects zero or greater-than-365-day keepalive periods and missing runtime
    /// context. The runtime must have its timer driver enabled.
    pub fn event_stream<S>(events: S, keepalive: Duration) -> io::Result<Self>
    where
        S: futures_core::Stream<Item = io::Result<String>> + Send + 'static,
    {
        let mut response = Self::new(200, Vec::new())?
            .with_header("content-type", "text/event-stream")?
            .with_header("cache-control", "no-cache")?;
        response.body = ServerBody::events(events, keepalive)?;
        Ok(response)
    }

    /// Encode SSE events with optional native `id:` and `event:` fields.
    /// The source remains pull-driven, and dropping the response drops it.
    /// Invalid field values cause a stream error before that event is sent.
    ///
    /// # Errors
    /// Rejects invalid keepalive periods or missing runtime context.
    pub fn sse_stream<S>(events: S, keepalive: Duration) -> io::Result<Self>
    where
        S: futures_core::Stream<Item = io::Result<SseEvent>> + Send + 'static,
    {
        let mut response = Self::new(200, Vec::new())?
            .with_header("content-type", "text/event-stream")?
            .with_header("cache-control", "no-cache")?;
        response.body = ServerBody::sse_events(events, keepalive)?;
        Ok(response)
    }

    /// Append an application header. Framing is always transport-owned.
    ///
    /// # Errors
    /// Rejects invalid syntax and connection/body-framing headers.
    pub fn with_header(mut self, name: &str, value: &str) -> io::Result<Self> {
        let name =
            hyper::header::HeaderName::from_bytes(name.as_bytes()).map_err(io::Error::other)?;
        if matches!(
            name.as_str(),
            "content-length"
                | "transfer-encoding"
                | "connection"
                | "upgrade"
                | "trailer"
                | "keep-alive"
                | "proxy-connection"
                | "te"
        ) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "HTTP framing header is transport-owned",
            ));
        }
        let value = hyper::header::HeaderValue::from_str(value).map_err(io::Error::other)?;
        self.headers.append(name, value);
        Ok(self)
    }
}

/// A bound listener. Serving uses the current runtime and never starts another.
#[derive(Debug)]
pub struct Server {
    listener: tokio::net::TcpListener,
    limits: Limits,
    diagnostics: Diagnostics,
    response_headers: Arc<hyper::HeaderMap>,
    read_budget: body::ReadBudget,
    streaming_requests: bool,
}

impl Server {
    /// Bind a caller-selected address after validating limits.
    ///
    /// # Errors
    /// Reports invalid limits and native listener errors.
    pub async fn bind(address: SocketAddr, limits: Limits) -> io::Result<Self> {
        limits.validate()?;
        Ok(Self {
            listener: tokio::net::TcpListener::bind(address).await?,
            limits,
            diagnostics: Diagnostics::default(),
            response_headers: Arc::new(hyper::HeaderMap::new()),
            read_budget: body::ReadBudget::new(limits.max_connections),
            streaming_requests: false,
        })
    }

    /// The bound address, including the assigned port when zero was requested.
    ///
    /// # Errors
    /// Reports a native socket inspection failure.
    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.listener.local_addr()
    }

    /// Shared, fixed-size counters; clone before moving this server into `serve`.
    pub fn diagnostics(&self) -> Diagnostics {
        self.diagnostics.clone()
    }

    /// Set an application-selected header on every dispatched response, replacing
    /// handler values with the same name. Includes body rejection and handler
    /// timeout responses. Does not affect errors generated by the HTTP parser
    /// before dispatch or a connection that closes without a response.
    ///
    /// No CORS policy is inferred: applications select origins, methods and
    /// preflight handling. Both configured and merged fields obey response limits.
    ///
    /// # Errors
    /// Rejects invalid/framing headers and configured headers exceeding limits.
    pub fn with_response_header(mut self, name: &str, value: &str) -> io::Result<Self> {
        let validated = Response::new(200, Vec::new())?.with_header(name, value)?;
        for (name, value) in &validated.headers {
            Arc::make_mut(&mut self.response_headers).insert(name.clone(), value.clone());
        }
        if !headers_fit(&self.response_headers, self.limits) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "server response headers exceed limits",
            ));
        }
        Ok(self)
    }

    /// Dispatch requests as soon as their headers arrive, with their bodies
    /// available through [`Request::next_chunk`]. By default the server
    /// buffers and validates each body before the handler runs.
    #[must_use]
    pub fn with_streaming_requests(mut self) -> Self {
        self.streaming_requests = true;
        self
    }

    /// Serve until cancelled or a listener error occurs. Connection tasks are
    /// owned by this future and cancelled on drop, including incomplete requests.
    ///
    /// A connection that upgrades to a WebSocket outlives its HTTP exchange:
    /// the connection task is held while the handed-off socket is in use, so it
    /// counts against `max_connections` and is not subject to
    /// `connection_timeout`, which bounds only the handshake. The upgraded
    /// connection's lifetime belongs to the handler that received it.
    ///
    /// # Errors
    /// Returns native listener errors; individual client failures are isolated.
    pub async fn serve<H, F>(self, handler: H) -> io::Result<()>
    where
        H: Fn(Request) -> F + Clone + Send + Sync + 'static,
        F: Future<Output = Response> + Send + 'static,
    {
        self.serve_with(move || handler.clone()).await
    }

    /// Like [`Self::serve`], constructing a handler for each accepted connection.
    /// Requests multiplexed on the same HTTP/2 connection share that handler.
    ///
    /// # Errors
    /// Returns native listener errors; individual client failures are isolated.
    pub async fn serve_with<M, H, F>(self, make_handler: M) -> io::Result<()>
    where
        M: Fn() -> H + Send + Sync,
        H: Fn(Request) -> F + Send + Sync + 'static,
        F: Future<Output = Response> + Send + 'static,
    {
        let mut tasks = tokio::task::JoinSet::new();
        loop {
            tokio::select! {
                result = tasks.join_next(), if !tasks.is_empty() => {
                    if let Some(Err(_)) = result {
                        increment(&self.diagnostics.0.task_failures);
                    }
                }
                accepted = self.listener.accept(), if tasks.len() < self.limits.max_connections => {
                    let (socket, _) = accepted?;
                    increment(&self.diagnostics.0.accepted_connections);
                    let config = ConnectionConfig {
                        limits: self.limits,
                        diagnostics: self.diagnostics.clone(),
                        response_headers: self.response_headers.clone(),
                        read_budget: self.read_budget.clone(),
                        streaming_requests: self.streaming_requests,
                    };
                    let handler = make_handler();
                    tasks.spawn(async move {
                        let _ = serve_on(socket, config, handler).await;
                    });
                }
            }
        }
    }
}

/// Serve HTTP/1.1 or prior-knowledge HTTP/2 on one caller-provided stream.
/// Any owned Tokio async read/write stream can be passed directly.
/// Request bodies are streamed through [`Request::next_chunk`].
/// Dropping the future cancels the connection and its in-flight requests.
/// With WebSocket support the connection remains owned until its upgrade task ends.
///
/// # Errors
/// Reports invalid limits, connection deadlines, transport and protocol errors.
pub async fn serve_connection<S, H, F>(stream: S, limits: Limits, handler: H) -> io::Result<()>
where
    S: Into<ConnectionStream> + Send,
    H: Fn(Request) -> F + Send + Sync + 'static,
    F: Future<Output = Response> + Send + 'static,
{
    limits.validate()?;
    serve_on(
        stream.into().into_inner(),
        ConnectionConfig {
            limits,
            diagnostics: Diagnostics::default(),
            response_headers: Arc::default(),
            read_budget: body::ReadBudget::new(limits.max_connections),
            streaming_requests: true,
        },
        handler,
    )
    .await
}

#[derive(Clone)]
struct ConnectionConfig {
    limits: Limits,
    diagnostics: Diagnostics,
    response_headers: Arc<hyper::HeaderMap>,
    read_budget: body::ReadBudget,
    streaming_requests: bool,
}

async fn serve_on<S, H, F>(stream: S, config: ConnectionConfig, handler: H) -> io::Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    H: Fn(Request) -> F + Send + Sync + 'static,
    F: Future<Output = Response> + Send + 'static,
{
    let limits = config.limits;
    let diagnostics = config.diagnostics.clone();
    let h2_tasks = Arc::new(std::sync::Mutex::new(tokio::task::JoinSet::new()));
    let mut builder = auto::Builder::new(transport::ConnectionExecutor::new(&h2_tasks));
    builder
        .http1()
        .timer(TokioTimer::new())
        .header_read_timeout(limits.header_timeout)
        .max_buf_size(limits.max_header_bytes)
        .max_headers(limits.max_headers);
    builder
        .http2()
        .max_header_list_size(limits.max_header_bytes as u32);
    #[cfg(feature = "websocket")]
    let (upgrades_tx, mut upgrades_rx) = tokio::sync::mpsc::unbounded_channel();
    let handler = Arc::new(handler);
    let service = service_fn(move |request| {
        dispatch(
            request,
            handler.clone(),
            config.clone(),
            #[cfg(feature = "websocket")]
            upgrades_tx.clone(),
        )
    });
    let stream = transport::ProgressIo::new(stream, limits.write_timeout)
        .with_detection_deadline(limits.header_timeout);
    let connection = builder.serve_connection_with_upgrades(TokioIo::new(stream), service);
    #[cfg(feature = "websocket")]
    let mut upgrade_tasks = tokio::task::JoinSet::new();
    #[cfg(feature = "websocket")]
    let outcome = tokio::time::timeout(limits.connection_timeout, async {
        tokio::pin!(connection);
        loop {
            tokio::select! {
                result = &mut connection => break result,
                Some(task) = upgrades_rx.recv() => { upgrade_tasks.spawn(task); }
                Some(result) = upgrade_tasks.join_next(), if !upgrade_tasks.is_empty() => {
                    if result.is_err() { increment(&diagnostics.0.task_failures); }
                }
            }
        }
    })
    .await;
    #[cfg(not(feature = "websocket"))]
    let outcome = tokio::time::timeout(limits.connection_timeout, connection).await;
    drop(h2_tasks);
    #[cfg(feature = "websocket")]
    {
        // Hyper resolves after the handshake. Keep successful upgrades owned
        // until their tasks finish, independently of the HTTP deadline.
        upgrades_rx.close();
        while let Ok(task) = upgrades_rx.try_recv() {
            upgrade_tasks.spawn(task);
        }
        while let Some(result) = upgrade_tasks.join_next().await {
            if result.is_err() {
                increment(&diagnostics.0.task_failures);
            }
        }
    }
    match outcome {
        Err(_) => {
            increment(&diagnostics.0.connection_timeouts);
            Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "HTTP connection deadline elapsed",
            ))
        }
        Ok(Err(error)) => {
            increment(&diagnostics.0.connection_errors);
            Err(error
                .downcast::<io::Error>()
                .map_or_else(io::Error::other, |error| *error))
        }
        Ok(Ok(())) => {
            increment(&diagnostics.0.completed_connections);
            Ok(())
        }
    }
}

fn empty(status: hyper::StatusCode) -> hyper::Response<ServerBody> {
    let mut response = hyper::Response::new(ServerBody::bytes(Bytes::new()));
    *response.status_mut() = status;
    response
}

async fn dispatch<H, F>(
    request: hyper::Request<Incoming>,
    handler: Arc<H>,
    config: ConnectionConfig,
    #[cfg(feature = "websocket")] upgrades_tx: tokio::sync::mpsc::UnboundedSender<UpgradeTask>,
) -> Result<hyper::Response<ServerBody>, Infallible>
where
    H: Fn(Request) -> F,
    F: Future<Output = Response>,
{
    let ConnectionConfig {
        limits,
        diagnostics,
        response_headers,
        read_budget,
        streaming_requests,
    } = config;
    let mut result = dispatch_inner(
        request,
        &*handler,
        limits,
        &diagnostics,
        streaming_requests,
        #[cfg(feature = "websocket")]
        upgrades_tx,
    )
    .await?;
    result.body_mut().set_read_budget(read_budget);
    for (name, value) in response_headers.iter() {
        result.headers_mut().insert(name.clone(), value.clone());
    }
    if !headers_fit(result.headers(), limits) {
        increment(&diagnostics.0.response_rejections);
        result = empty(hyper::StatusCode::INTERNAL_SERVER_ERROR);
        *result.headers_mut() = (*response_headers).clone();
    }
    Ok(result)
}

fn headers_fit(headers: &hyper::HeaderMap, limits: Limits) -> bool {
    let bytes = headers.iter().try_fold(0usize, |total, (name, value)| {
        total
            .checked_add(name.as_str().len())?
            .checked_add(value.as_bytes().len())?
            .checked_add(4)
    });
    headers.len() <= limits.max_response_headers
        && bytes.is_some_and(|bytes| bytes <= limits.max_response_header_bytes)
}

async fn dispatch_inner<H, F>(
    request: hyper::Request<Incoming>,
    handler: H,
    limits: Limits,
    diagnostics: &Diagnostics,
    streaming_requests: bool,
    #[cfg(feature = "websocket")] upgrades_tx: tokio::sync::mpsc::UnboundedSender<UpgradeTask>,
) -> Result<hyper::Response<ServerBody>, Infallible>
where
    H: Fn(Request) -> F,
    F: Future<Output = Response>,
{
    #[cfg(feature = "websocket")]
    let mut request = request;
    #[cfg(feature = "websocket")]
    let upgrade = hyper::upgrade::on(&mut request);
    let (parts, body) = request.into_parts();
    let h2 = parts.version == hyper::Version::HTTP_2;
    let h2_head = h2 && parts.method == hyper::Method::HEAD;
    if h2 && parts.headers.len() > limits.max_headers {
        increment(&diagnostics.0.request_rejections);
        return Ok(empty(hyper::StatusCode::REQUEST_HEADER_FIELDS_TOO_LARGE));
    }
    let (body, streaming_body) = if streaming_requests {
        use hyper::body::Body as _;
        if body.size_hint().lower() > limits.max_request_body_bytes as u64 {
            increment(&diagnostics.0.request_rejections);
            return Ok(empty(hyper::StatusCode::PAYLOAD_TOO_LARGE));
        }
        (
            Vec::new(),
            Some(request_body::IncomingBody::new(
                body,
                limits,
                diagnostics.clone(),
            )),
        )
    } else {
        match tokio::time::timeout(
            limits.body_timeout,
            Limited::new(body, limits.max_request_body_bytes).collect(),
        )
        .await
        {
            Err(_) => {
                increment(&diagnostics.0.body_timeouts);
                return Ok(empty(hyper::StatusCode::REQUEST_TIMEOUT));
            }
            Ok(Err(error)) => {
                increment(&diagnostics.0.request_rejections);
                return Ok(empty(if error.is::<http_body_util::LengthLimitError>() {
                    hyper::StatusCode::PAYLOAD_TOO_LARGE
                } else {
                    hyper::StatusCode::BAD_REQUEST
                }));
            }
            Ok(Ok(body)) => (body.to_bytes().to_vec(), None),
        }
    };
    let request = Request {
        method: parts.method.to_string(),
        #[cfg(feature = "websocket")]
        http_1_1: parts.version == hyper::Version::HTTP_11,
        target: parts.uri.to_string(),
        uri: parts.uri,
        headers: parts.headers,
        body,
        streaming_body,
        #[cfg(feature = "websocket")]
        upgrade: Some(upgrade),
    };
    let mut response = match tokio::time::timeout(limits.handler_timeout, handler(request)).await {
        Ok(response) => response,
        Err(_) => {
            increment(&diagnostics.0.handler_timeouts);
            return Ok(empty(hyper::StatusCode::GATEWAY_TIMEOUT));
        }
    };
    #[cfg(feature = "websocket")]
    if let Some(upgrade_task) = response.upgrade_task.take() {
        let _ = upgrades_tx.send(upgrade_task);
    }
    if !headers_fit(&response.headers, limits) || response.body.configure(limits).is_err() {
        increment(&diagnostics.0.response_rejections);
        return Ok(empty(hyper::StatusCode::INTERNAL_SERVER_ERROR));
    }
    if h2_head {
        use hyper::body::Body as _;
        if let Some(length) = response
            .body
            .size_hint()
            .exact()
            .filter(|&length| length > 0)
        {
            response
                .headers
                .insert(hyper::header::CONTENT_LENGTH, length.into());
        }
        response.body = ServerBody::bytes(Bytes::new());
    }
    let mut result = hyper::Response::new(response.body);
    *result.status_mut() = response.status;
    *result.headers_mut() = response.headers;
    Ok(result)
}
