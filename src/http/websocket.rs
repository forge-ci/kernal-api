//! WebSocket client over the HTTP client's TCP and verified TLS connectors.

use super::{connector, timed_out, transport, Limits, Target};
use crate::websocket::SocketIo;
use hyper::header::{HeaderName, HeaderValue};
use hyper_util::rt::TokioIo;
use std::{future::poll_fn, io};
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tower_service::Service;

pub use crate::websocket::{
    Message, WebSocket, WebSocketLimits, WebSocketReceiver, WebSocketSender,
};

/// Open a `ws://` or `wss://` connection with application headers and message limits.
///
/// The HTTP/1.1 handshake uses the HTTP client's default connect timeout.
/// TLS uses bundled rustls roots, or the platform trust store when `http-client`
/// is enabled. Redirects are not followed.
pub async fn connect(
    url: &str,
    headers: &[(&str, &str)],
    limits: WebSocketLimits,
) -> io::Result<WebSocket> {
    connect_with_root(url, headers, limits, None).await
}

pub(crate) async fn connect_with_root(
    url: &str,
    headers: &[(&str, &str)],
    limits: WebSocketLimits,
    extra_root: Option<&[u8]>,
) -> io::Result<WebSocket> {
    let config = limits.config()?;
    let http_limits = Limits::default();
    if url.len() > http_limits.max_url_bytes || url.contains('#') {
        return Err(invalid(
            "WebSocket URL exceeds the URL limit or contains a fragment",
        ));
    }
    let Some((scheme, target)) = url.split_once("://") else {
        return Err(invalid("expected an absolute ws or wss URL"));
    };
    let (scheme, http_scheme) = match scheme.to_ascii_lowercase().as_str() {
        "ws" => ("ws", "http"),
        "wss" => ("wss", "https"),
        _ => return Err(invalid("expected a ws or wss URL")),
    };
    let uri = Target::parse(&format!("{http_scheme}://{target}"))
        .and_then(|target| target.request_uri())
        .map_err(|_| invalid("invalid WebSocket URL"))?;
    let mut parts = uri.clone().into_parts();
    parts.scheme = Some(scheme.parse().map_err(transport)?);
    let request_uri = hyper::Uri::from_parts(parts).map_err(transport)?;
    let mut request = request_uri.into_client_request().map_err(transport)?;
    let mut remaining = http_limits.max_header_bytes;
    if headers.len() > http_limits.max_header_count {
        return Err(invalid("too many WebSocket request headers"));
    }
    for (name, value) in headers {
        remaining = remaining
            .checked_sub(name.len())
            .and_then(|remaining| remaining.checked_sub(value.len()))
            .ok_or_else(|| invalid("WebSocket request headers exceed the byte limit"))?;
        let name = HeaderName::from_bytes(name.as_bytes())
            .map_err(|_| invalid("invalid WebSocket request header name"))?;
        if matches!(
            name.as_str(),
            "host"
                | "connection"
                | "upgrade"
                | "content-length"
                | "transfer-encoding"
                | "proxy-authorization"
                | "sec-websocket-key"
                | "sec-websocket-version"
                | "sec-websocket-extensions"
                | "sec-websocket-accept"
        ) {
            return Err(invalid(
                "WebSocket framing and proxy headers are transport-owned",
            ));
        }
        let value = HeaderValue::from_str(value)
            .map_err(|_| invalid("invalid WebSocket request header value"))?;
        request.headers_mut().append(name, value);
    }
    tokio::time::timeout(http_limits.connect_timeout, async {
        let mut connector = connector(extra_root, false)?;
        poll_fn(|cx| connector.poll_ready(cx))
            .await
            .map_err(transport)?;
        let stream = connector.call(uri).await.map_err(transport)?;
        let stream = Box::new(TokioIo::new(stream)) as Box<dyn SocketIo>;
        let (socket, _) =
            tokio_tungstenite::client_async_with_config(request, stream, Some(config))
                .await
                .map_err(transport)?;
        Ok(WebSocket::new(socket, limits))
    })
    .await
    .map_err(|_| timed_out("WebSocket handshake timed out"))?
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
