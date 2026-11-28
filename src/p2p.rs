//! Authenticated peer-to-peer QUIC connections with optional relay assistance.

use std::fmt;
use std::io;
use std::net::SocketAddr;
use std::pin::Pin;
use std::str::FromStr;
use std::task::{Context, Poll};
use std::time::Duration;

use futures_util::StreamExt;
use iroh::endpoint::{presets, PathList, PathListStream, QuicTransportConfig};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};

/// A private peer identity backed by an Ed25519 seed.
#[derive(Clone)]
pub struct SecretKey(iroh::SecretKey);

impl SecretKey {
    /// Reconstruct an identity from a persisted seed.
    pub fn from_bytes(seed: &[u8; 32]) -> Self {
        Self(iroh::SecretKey::from_bytes(seed))
    }

    /// Generate a fresh identity using system randomness.
    pub fn generate() -> Self {
        Self(iroh::SecretKey::generate())
    }

    /// Export the private seed. Store it as a credential.
    pub fn to_bytes(&self) -> [u8; 32] {
        self.0.to_bytes()
    }

    /// Return the public peer identity.
    pub fn public(&self) -> PeerId {
        PeerId(self.0.public())
    }
}

/// The public key used to identify and authenticate a peer.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct PeerId(iroh::EndpointId);

impl fmt::Display for PeerId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&self.0, f)
    }
}

impl FromStr for PeerId {
    type Err = io::Error;

    fn from_str(value: &str) -> io::Result<Self> {
        value
            .parse()
            .map(Self)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))
    }
}

/// Which transports an endpoint can use.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum RelayMode {
    /// Use direct UDP and, when configured, a relay.
    #[default]
    Default,
    /// Use only the configured relay.
    RelayOnly,
}

/// QUIC idle and keep-alive settings for a bound endpoint.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TransportConfig {
    /// Maximum negotiated idle time. Zero disables the local idle timeout.
    pub idle_timeout: Duration,
    /// Interval for both connection and default-path keep-alive, in (0, 5s].
    pub keep_alive_interval: Duration,
}

impl Default for TransportConfig {
    fn default() -> Self {
        Self {
            idle_timeout: Duration::from_secs(30),
            keep_alive_interval: Duration::from_secs(2),
        }
    }
}

/// A local peer that can dial and accept authenticated QUIC connections.
#[derive(Clone, Debug)]
pub struct Endpoint {
    inner: iroh::Endpoint,
    relay: Option<iroh::RelayUrl>,
}

impl Endpoint {
    /// Bind with the default QUIC settings. Pass `None` for direct UDP only.
    /// Call [`Self::set_alpns`] before accepting connections.
    pub async fn bind(
        key: SecretKey,
        relay_url: Option<&str>,
        relay_mode: RelayMode,
    ) -> io::Result<Self> {
        Self::bind_with_config(key, relay_url, relay_mode, TransportConfig::default()).await
    }

    /// Bind with explicit QUIC idle and keep-alive settings.
    pub async fn bind_with_config(
        key: SecretKey,
        relay_url: Option<&str>,
        relay_mode: RelayMode,
        config: TransportConfig,
    ) -> io::Result<Self> {
        if config.keep_alive_interval.is_zero()
            || config.keep_alive_interval > Duration::from_secs(5)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "keep-alive interval must be greater than zero and at most five seconds",
            ));
        }
        let relay: Option<iroh::RelayUrl> = relay_url
            .map(str::parse)
            .transpose()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        if relay_mode == RelayMode::RelayOnly && relay.is_none() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "relay-only endpoints require a relay URL",
            ));
        }
        let idle_timeout = config
            .idle_timeout
            .try_into()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
        let transport = QuicTransportConfig::builder()
            .max_idle_timeout(Some(idle_timeout))
            .keep_alive_interval(config.keep_alive_interval)
            .default_path_keep_alive_interval(config.keep_alive_interval)
            .build();
        let mut builder = iroh::Endpoint::builder(presets::Minimal)
            .secret_key(key.0)
            .clear_address_lookup()
            .relay_mode(match &relay {
                Some(url) => iroh::RelayMode::custom([url.clone()]),
                None => iroh::RelayMode::Disabled,
            })
            .transport_config(transport);
        if relay_mode == RelayMode::RelayOnly {
            builder = builder.clear_ip_transports();
        }
        let inner = builder.bind().await.map_err(io::Error::other)?;
        Ok(Self { inner, relay })
    }

    /// Replace the accepted application protocol identifiers. Outbound
    /// connections supply their own protocol identifier.
    pub fn set_alpns(&self, protocols: &[&[u8]]) {
        self.inner
            .set_alpns(protocols.iter().map(|protocol| protocol.to_vec()).collect());
    }

    /// Return this endpoint's public identity.
    pub fn id(&self) -> PeerId {
        PeerId(self.inner.id())
    }

    /// Return locally bound UDP sockets, which may have wildcard addresses.
    pub fn bound_sockets(&self) -> Vec<SocketAddr> {
        self.inner.bound_sockets()
    }

    /// Dial a peer using this endpoint's configured relay as the initial
    /// address. With direct UDP enabled the connection can upgrade to it.
    pub async fn connect(&self, peer: PeerId, alpn: &[u8]) -> io::Result<Connection> {
        let relay = self.relay.clone().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "connecting by peer ID requires a relay URL or an explicit socket address",
            )
        })?;
        let address = iroh::EndpointAddr::new(peer.0).with_relay_url(relay);
        self.inner
            .connect(address, alpn)
            .await
            .map(Connection)
            .map_err(io::Error::other)
    }

    /// Dial a known UDP address while authenticating the remote peer's key.
    pub async fn connect_direct(
        &self,
        peer: PeerId,
        socket: SocketAddr,
        alpn: &[u8],
    ) -> io::Result<Connection> {
        let address = iroh::EndpointAddr::new(peer.0).with_ip_addr(socket);
        self.inner
            .connect(address, alpn)
            .await
            .map(Connection)
            .map_err(io::Error::other)
    }

    /// Accept and complete one incoming handshake. Returns `None` after the
    /// endpoint closes. Use [`Self::accept_incoming`] for concurrent handshakes.
    pub async fn accept(&self) -> Option<io::Result<Connection>> {
        Some(self.accept_incoming().await?.handshake().await)
    }

    /// Receive an incoming attempt without waiting for its handshake.
    pub async fn accept_incoming(&self) -> Option<Incoming> {
        self.inner.accept().await.map(Incoming)
    }

    /// Close this endpoint and all its connections.
    pub async fn close(&self) {
        self.inner.close().await;
    }
}

/// An incoming connection attempt whose peer has yet to be authenticated.
pub struct Incoming(iroh::endpoint::Incoming);

impl Incoming {
    /// Complete the handshake and authenticate the remote peer.
    pub async fn handshake(self) -> io::Result<Connection> {
        self.0.await.map(Connection).map_err(io::Error::other)
    }
}

/// An authenticated peer connection carrying independent QUIC streams.
#[derive(Clone, Debug)]
pub struct Connection(iroh::endpoint::Connection);

impl Connection {
    /// Return the authenticated remote identity.
    pub fn remote_id(&self) -> PeerId {
        PeerId(self.0.remote_id())
    }

    /// Return the negotiated application protocol identifier.
    pub fn alpn(&self) -> &[u8] {
        self.0.alpn()
    }

    /// Open a bidirectional stream. The peer can accept it once data or a
    /// stream close has been sent.
    pub async fn open_bi(&self) -> io::Result<(SendStream, RecvStream)> {
        let (send, recv) = self.0.open_bi().await.map_err(io::Error::other)?;
        Ok((SendStream(send), RecvStream(recv)))
    }

    /// Accept a bidirectional stream initiated by the peer.
    pub async fn accept_bi(&self) -> io::Result<(SendStream, RecvStream)> {
        let (send, recv) = self.0.accept_bi().await.map_err(io::Error::other)?;
        Ok((SendStream(send), RecvStream(recv)))
    }

    /// Open a send-only stream.
    pub async fn open_uni(&self) -> io::Result<SendStream> {
        self.0
            .open_uni()
            .await
            .map(SendStream)
            .map_err(io::Error::other)
    }

    /// Accept a receive-only stream initiated by the peer.
    pub async fn accept_uni(&self) -> io::Result<RecvStream> {
        self.0
            .accept_uni()
            .await
            .map(RecvStream)
            .map_err(io::Error::other)
    }

    /// Return the selected transport, if a path is currently selected.
    pub fn path(&self) -> Option<Path> {
        selected_path(&self.0.paths())
    }

    /// Watch for changes to the selected transport.
    pub fn observe_path(&self) -> PathObserver<'_> {
        PathObserver {
            paths: self.0.paths_stream(),
            previous: None,
        }
    }

    /// Close the connection with an application-defined code and reason.
    pub fn close(&self, code: u32, reason: &[u8]) {
        self.0.close(code.into(), reason);
    }

    /// Wait until the connection closes.
    pub async fn closed(&self) {
        self.0.closed().await;
    }
}

/// The transport selected for a peer connection.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Path {
    /// Direct UDP to the peer.
    Direct,
    /// Traffic through a relay.
    Relay,
}

fn selected_path(paths: &PathList<'_>) -> Option<Path> {
    paths.iter().find(|path| path.is_selected()).map(|path| {
        if path.is_ip() {
            Path::Direct
        } else {
            Path::Relay
        }
    })
}

/// Observes the initial selected transport and subsequent changes.
pub struct PathObserver<'a> {
    paths: PathListStream<'a>,
    previous: Option<Path>,
}

impl PathObserver<'_> {
    /// Wait for a different selected transport, or return `None` when the
    /// connection closes. This wait can be cancelled and resumed.
    pub async fn next_path(&mut self) -> Option<Path> {
        while let Some(paths) = self.paths.next().await {
            let Some(path) = selected_path(&paths) else {
                continue;
            };
            if self.previous.replace(path) != Some(path) {
                return Some(path);
            }
        }
        None
    }
}

/// The writable half of a QUIC stream.
#[derive(Debug)]
pub struct SendStream(iroh::endpoint::SendStream);

impl SendStream {
    /// Write a buffer, returning the number of bytes accepted.
    pub async fn write(&mut self, data: &[u8]) -> io::Result<usize> {
        self.0.write(data).await.map_err(io::Error::from)
    }

    /// Write the entire buffer.
    pub async fn write_all(&mut self, data: &[u8]) -> io::Result<()> {
        self.0.write_all(data).await.map_err(io::Error::from)
    }

    /// Finish sending. The peer reads EOF after the buffered data.
    pub fn finish(&mut self) -> io::Result<()> {
        self.0.finish().map_err(io::Error::other)
    }
}

impl AsyncWrite for SendStream {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        data: &[u8],
    ) -> Poll<io::Result<usize>> {
        AsyncWrite::poll_write(Pin::new(&mut self.get_mut().0), cx, data)
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        AsyncWrite::poll_flush(Pin::new(&mut self.get_mut().0), cx)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        AsyncWrite::poll_shutdown(Pin::new(&mut self.get_mut().0), cx)
    }
}

/// The readable half of a QUIC stream.
#[derive(Debug)]
pub struct RecvStream(iroh::endpoint::RecvStream);

impl RecvStream {
    /// Read into a buffer. Returns `None` at end of stream.
    pub async fn read(&mut self, buffer: &mut [u8]) -> io::Result<Option<usize>> {
        self.0.read(buffer).await.map_err(io::Error::from)
    }

    /// Read the stream to EOF with a maximum size in bytes.
    pub async fn read_to_end(&mut self, limit: usize) -> io::Result<Vec<u8>> {
        self.0.read_to_end(limit).await.map_err(io::Error::other)
    }
}

impl AsyncRead for RecvStream {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        AsyncRead::poll_read(Pin::new(&mut self.get_mut().0), cx, buffer)
    }
}
