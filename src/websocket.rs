//! Shared WebSocket facade and bounded message transport.

use futures_util::{
    stream::{SplitSink, SplitStream},
    SinkExt, StreamExt,
};
use std::io;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio_tungstenite::{tungstenite, WebSocketStream};

pub(crate) trait SocketIo: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> SocketIo for T {}

type SocketStream = WebSocketStream<Box<dyn SocketIo>>;

/// Per-connection WebSocket acceptance limits.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WebSocketLimits {
    /// Maximum decoded message payload bytes.
    pub max_message_bytes: usize,
    /// Maximum single-frame payload bytes.
    pub max_frame_bytes: usize,
}

impl Default for WebSocketLimits {
    fn default() -> Self {
        Self {
            max_message_bytes: 64 * 1024,
            max_frame_bytes: 64 * 1024,
        }
    }
}

impl WebSocketLimits {
    pub(crate) fn config(self) -> io::Result<tungstenite::protocol::WebSocketConfig> {
        if !(1..=64 * 1024 * 1024).contains(&self.max_message_bytes)
            || !(1..=self.max_message_bytes).contains(&self.max_frame_bytes)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid WebSocket message/frame limits",
            ));
        }
        Ok(tungstenite::protocol::WebSocketConfig::default()
            .max_message_size(Some(self.max_message_bytes))
            .max_frame_size(Some(self.max_frame_bytes)))
    }
}

/// Facade-owned WebSocket message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Message {
    Text(String),
    Binary(Vec<u8>),
    Ping(Vec<u8>),
    Pong(Vec<u8>),
    Close,
}

fn from_transport(message: tungstenite::Message) -> Message {
    match message {
        tungstenite::Message::Text(text) => Message::Text(text.to_string()),
        tungstenite::Message::Binary(data) => Message::Binary(data.to_vec()),
        tungstenite::Message::Ping(data) => Message::Ping(data.to_vec()),
        tungstenite::Message::Pong(data) => Message::Pong(data.to_vec()),
        tungstenite::Message::Close(_) => Message::Close,
        tungstenite::Message::Frame(_) => Message::Close,
    }
}

fn into_transport(message: Message) -> tungstenite::Message {
    match message {
        Message::Text(text) => tungstenite::Message::Text(text.into()),
        Message::Binary(data) => tungstenite::Message::Binary(data.into()),
        Message::Ping(data) => tungstenite::Message::Ping(data.into()),
        Message::Pong(data) => tungstenite::Message::Pong(data.into()),
        Message::Close => tungstenite::Message::Close(None),
    }
}

/// An upgraded connection with bounded facade messages.
pub struct WebSocket {
    inner: SocketStream,
    limits: WebSocketLimits,
}

/// Write half of an upgraded connection. It retains the connection's bounded
/// outbound-message policy after [`WebSocket::split`].
pub struct WebSocketSender {
    inner: SplitSink<SocketStream, tungstenite::Message>,
    limits: WebSocketLimits,
}

/// Read half of an upgraded connection, returned by [`WebSocket::split`].
pub struct WebSocketReceiver {
    inner: SplitStream<SocketStream>,
}

impl WebSocket {
    pub(crate) fn new(inner: SocketStream, limits: WebSocketLimits) -> Self {
        Self { inner, limits }
    }

    /// Receive one message. `Ok(None)` means the peer closed cleanly.
    pub async fn receive(&mut self) -> io::Result<Option<Message>> {
        match self.inner.next().await {
            Some(Ok(message)) => Ok(Some(from_transport(message))),
            Some(Err(error)) => Err(io::Error::other(error)),
            None => Ok(None),
        }
    }

    /// Send one message, flushing it to the connection.
    pub async fn send(&mut self, message: Message) -> io::Result<()> {
        validate_outgoing(self.limits, &message)?;
        self.inner
            .send(into_transport(message))
            .await
            .map_err(io::Error::other)
    }

    /// Split this connection into independently owned read and write halves.
    /// Both halves are cancelled when their owning tasks are dropped.
    pub fn split(self) -> (WebSocketSender, WebSocketReceiver) {
        let (inner, receiver) = self.inner.split();
        (
            WebSocketSender {
                inner,
                limits: self.limits,
            },
            WebSocketReceiver { inner: receiver },
        )
    }
}

impl WebSocketSender {
    /// Send one bounded message and flush it to the peer.
    pub async fn send(&mut self, message: Message) -> io::Result<()> {
        validate_outgoing(self.limits, &message)?;
        self.inner
            .send(into_transport(message))
            .await
            .map_err(io::Error::other)
    }
}

impl WebSocketReceiver {
    /// Receive one message. `Ok(None)` means the peer closed cleanly.
    pub async fn receive(&mut self) -> io::Result<Option<Message>> {
        match self.inner.next().await {
            Some(Ok(message)) => Ok(Some(from_transport(message))),
            Some(Err(error)) => Err(io::Error::other(error)),
            None => Ok(None),
        }
    }
}

fn validate_outgoing(limits: WebSocketLimits, message: &Message) -> io::Result<()> {
    let bytes = match message {
        Message::Text(text) => text.len(),
        Message::Binary(data) | Message::Ping(data) | Message::Pong(data) => data.len(),
        Message::Close => 0,
    };
    if bytes > limits.max_message_bytes
        || bytes > limits.max_frame_bytes
        || (matches!(message, Message::Ping(_) | Message::Pong(_)) && bytes > 125)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "WebSocket outgoing message exceeds configured limits",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limits_are_bounded_and_frame_cannot_exceed_message() {
        assert!(WebSocketLimits::default().config().is_ok());
        assert!(WebSocketLimits {
            max_message_bytes: 0,
            max_frame_bytes: 1,
        }
        .config()
        .is_err());
        assert!(WebSocketLimits {
            max_message_bytes: 10,
            max_frame_bytes: 11,
        }
        .config()
        .is_err());
    }

    #[test]
    fn facade_messages_do_not_expose_transport_values() {
        let message = Message::Binary(vec![1, 2, 3]);
        assert_eq!(from_transport(into_transport(message.clone())), message);
    }
}
