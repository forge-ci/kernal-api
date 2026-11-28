#![cfg(all(feature = "websocket-client", feature = "websocket"))]

use kernal_api::http::websocket::{connect, Message, WebSocketLimits};
use kernal_api::http_server::{Limits, Request, Response, Server, WebSocket};
use std::{io, time::Duration};

#[tokio::test]
async fn exchanges_authenticated_messages_and_closes() {
    let server = Server::bind("127.0.0.1:0".parse().unwrap(), Limits::default())
        .await
        .unwrap();
    let url = format!("ws://{}/socket?mode=echo", server.local_addr().unwrap());
    let task = tokio::spawn(server.serve(|request: Request| async move {
        if request.target() != "/socket?mode=echo"
            || request.header("authorization") != Some(b"Bearer fixture".as_slice())
        {
            return Response::new(401, Vec::new()).unwrap();
        }
        request
            .into_websocket()
            .unwrap()
            .on_upgrade(WebSocketLimits::default(), |mut socket| async move {
                for _ in 0..2 {
                    let message = socket.receive().await.unwrap().unwrap();
                    socket.send(message).await.unwrap();
                }
                assert_eq!(
                    socket.receive().await.unwrap(),
                    Some(Message::Ping(vec![7]))
                );
                assert_eq!(socket.receive().await.unwrap(), Some(Message::Close));
                assert_eq!(socket.receive().await.unwrap(), None);
            })
            .unwrap()
    }));

    tokio::time::timeout(Duration::from_secs(5), async {
        let socket: WebSocket = connect(
            &url,
            &[("Authorization", "Bearer fixture")],
            WebSocketLimits::default(),
        )
        .await
        .unwrap();
        let (mut sender, mut receiver) = socket.split();
        for message in [
            Message::Binary(vec![0, 42, 255]),
            Message::Text("hello".into()),
        ] {
            sender.send(message.clone()).await.unwrap();
            assert_eq!(receiver.receive().await.unwrap(), Some(message));
        }
        sender.send(Message::Ping(vec![7])).await.unwrap();
        assert_eq!(
            receiver.receive().await.unwrap(),
            Some(Message::Pong(vec![7]))
        );
        sender.send(Message::Close).await.unwrap();
        assert_eq!(receiver.receive().await.unwrap(), Some(Message::Close));
        assert_eq!(receiver.receive().await.unwrap(), None);
    })
    .await
    .unwrap();
    task.abort();
}

#[tokio::test]
async fn enforces_client_frame_limits_in_both_directions() {
    let server = Server::bind("127.0.0.1:0".parse().unwrap(), Limits::default())
        .await
        .unwrap();
    let url = format!("ws://{}/socket", server.local_addr().unwrap());
    let task = tokio::spawn(server.serve(|request: Request| async move {
        request
            .into_websocket()
            .unwrap()
            .on_upgrade(WebSocketLimits::default(), |mut socket| async move {
                let message = socket.receive().await.unwrap().unwrap();
                socket.send(message).await.unwrap();
                socket.send(Message::Binary(vec![0; 9])).await.unwrap();
            })
            .unwrap()
    }));

    tokio::time::timeout(Duration::from_secs(5), async {
        let limits = WebSocketLimits {
            max_message_bytes: 16,
            max_frame_bytes: 8,
        };
        let mut socket = connect(&url, &[], limits).await.unwrap();
        let error = socket.send(Message::Binary(vec![0; 9])).await.unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        let message = Message::Binary(vec![1; 8]);
        socket.send(message.clone()).await.unwrap();
        assert_eq!(socket.receive().await.unwrap(), Some(message));
        assert!(socket.receive().await.is_err());
    })
    .await
    .unwrap();
    task.abort();
}

#[tokio::test]
async fn rejects_transport_headers_and_invalid_urls() {
    let limits = WebSocketLimits::default();
    for (url, headers) in [
        ("http://localhost/socket", &[][..]),
        ("ws://user@localhost/socket", &[][..]),
        ("ws://localhost/socket#fragment", &[][..]),
        (
            "ws://localhost/socket",
            &[("Sec-WebSocket-Key", "override")][..],
        ),
        (
            "ws://localhost/socket",
            &[("Authorization", "invalid\r\nheader")][..],
        ),
    ] {
        let result = connect(url, headers, limits).await;
        assert_eq!(result.err().unwrap().kind(), io::ErrorKind::InvalidInput);
    }
}
