#![cfg(feature = "http-server")]

use kernal_api::{
    async_engine,
    http_server::{Limits, Response, Server},
};
use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr},
    time::Duration,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

fn loopback() -> SocketAddr {
    SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0)
}

#[tokio::test]
async fn file_preparation_is_bounded_and_rejects_non_files() {
    use kernal_api::http_server::FileResponses;
    let files = FileResponses::new(2, Duration::from_secs(1)).unwrap();
    let root = tempfile::tempdir().unwrap();
    assert!(files
        .open(root.path().to_path_buf(), Vec::new())
        .await
        .is_err());
    let path = root.path().join("data");
    std::fs::write(&path, b"hello").unwrap();
    files.open(path, b"prefix".to_vec()).await.unwrap();
    assert!(FileResponses::new(0, Duration::from_secs(1)).is_err());
}

#[cfg(target_os = "linux")]
#[tokio::test]
async fn fifo_preparation_does_not_block_executor_progress() {
    let root = tempfile::tempdir().unwrap();
    let path = root.path().join("pipe");
    assert!(std::process::Command::new("mkfifo")
        .arg(&path)
        .status()
        .unwrap()
        .success());
    // A watchdog opens both ends on Linux, unblocking an accidental synchronous
    // FIFO open so a regression fails rather than hanging runtime teardown.
    let unblock = path.clone();
    let watchdog = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(500));
        let _guard = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(unblock)
            .unwrap();
    });
    let files = kernal_api::http_server::FileResponses::new(1, Duration::from_secs(1)).unwrap();
    let start = std::time::Instant::now();
    assert!(files.open(path, Vec::new()).await.is_err());
    assert!(start.elapsed() < Duration::from_millis(400));
    watchdog.join().unwrap();
}

#[tokio::test]
async fn default_response_is_an_empty_internal_error() {
    let server = Server::bind(loopback(), Limits::default()).await.unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(|_| async { Response::default() }));
    let response = exchange(
        addr,
        b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(response.starts_with("HTTP/1.1 500"), "{response}");
    assert!(response.ends_with("\r\n\r\n"), "{response}");
    drop(task);
}

#[tokio::test]
async fn server_headers_apply_to_generated_errors_and_override_handler_values() {
    let server = Server::bind(
        loopback(),
        Limits {
            max_request_body_bytes: 1,
            max_response_body_bytes: 1,
            body_timeout: Duration::from_millis(10),
            handler_timeout: Duration::from_millis(10),
            ..Limits::default()
        },
    )
    .await
    .unwrap()
    .with_response_header("x-policy", "server")
    .unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(|request| async move {
        if request.target() == "/timeout" {
            std::future::pending::<()>().await;
        }
        let body = if request.target() == "/large" {
            "xx"
        } else {
            "x"
        };
        Response::new(200, body.as_bytes().to_vec())
            .unwrap()
            .with_header("x-policy", "handler")
            .unwrap()
    }));
    for (target, length, body, status) in [
        ("/", 0, "", 200),
        ("/large", 0, "", 500),
        ("/timeout", 0, "", 504),
        ("/", 2, "xx", 413),
        ("/", 1, "", 408),
    ] {
        let request = format!("POST {target} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {length}\r\nConnection: close\r\n\r\n{body}");
        let response = exchange(addr, request.as_bytes()).await;
        assert!(
            response.starts_with(&format!("HTTP/1.1 {status}")),
            "{response}"
        );
        assert!(response.contains("x-policy: server\r\n"), "{response}");
        assert!(!response.contains("x-policy: handler"), "{response}");
    }
    drop(task);
}

#[tokio::test]
async fn merged_response_headers_stay_within_the_acceptance_budget() {
    let server = Server::bind(
        loopback(),
        Limits {
            max_response_headers: 1,
            ..Limits::default()
        },
    )
    .await
    .unwrap()
    .with_response_header("x-policy", "server")
    .unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(|_| async {
        Response::new(200, Vec::new())
            .unwrap()
            .with_header("extra", "value")
            .unwrap()
    }));
    let response = exchange(
        addr,
        b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(response.starts_with("HTTP/1.1 500"), "{response}");
    assert!(response.contains("x-policy: server\r\n"), "{response}");
    assert!(!response.contains("extra:"));
    drop(task);
    assert!(Server::bind(
        loopback(),
        Limits {
            max_response_headers: 0,
            ..Limits::default()
        }
    )
    .await
    .unwrap()
    .with_response_header("x", "y")
    .is_err());
    assert!(Server::bind(loopback(), Limits::default())
        .await
        .unwrap()
        .with_response_header("content-length", "0")
        .is_err());
}

#[tokio::test]
async fn request_path_and_query_decode_without_hiding_duplicates() {
    let server = Server::bind(loopback(), Limits::default()).await.unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(|request| async move {
        assert_eq!(request.path(), "/some%20file+name");
        assert_eq!(request.decoded_path().unwrap(), "/some file+name");
        assert_eq!(
            request.query(),
            Some("name=first+value&name=%E2%98%83&empty&x=a%26b%3Dc&&")
        );
        let pairs = request
            .query_pairs()
            .collect::<std::io::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(
            pairs,
            [
                ("name".into(), "first value".into()),
                ("name".into(), "☃".into()),
                ("empty".into(), "".into()),
                ("x".into(), "a&b=c".into())
            ]
        );
        Response::new(200, Vec::new()).unwrap()
    }));
    let response = exchange(addr, b"GET /some%20file+name?name=first+value&name=%E2%98%83&empty&x=a%26b%3Dc&& HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n").await;
    assert!(response.starts_with("HTTP/1.1 200"), "{response}");
    drop(task);
}

#[tokio::test]
async fn head_suppresses_stream_polling_and_options_reaches_product_handler() {
    struct NeverPoll;
    impl futures_core::Stream for NeverPoll {
        type Item = std::io::Result<String>;
        fn poll_next(
            self: std::pin::Pin<&mut Self>,
            _: &mut std::task::Context<'_>,
        ) -> std::task::Poll<Option<Self::Item>> {
            panic!("HEAD must not consume an event source");
        }
    }
    let server = Server::bind(loopback(), Limits::default()).await.unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(|request| async move {
        match request.method() {
            "HEAD" => Response::event_stream(NeverPoll, Duration::from_secs(1)).unwrap(),
            "OPTIONS" => Response::new(204, Vec::new())
                .unwrap()
                .with_header("allow", "GET, HEAD, OPTIONS")
                .unwrap(),
            _ => panic!("unexpected method"),
        }
    }));
    for (method, status) in [("HEAD", 200), ("OPTIONS", 204)] {
        let request =
            format!("{method} / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n");
        let response = exchange(addr, request.as_bytes()).await;
        assert!(
            response.starts_with(&format!("HTTP/1.1 {status}")),
            "{response}"
        );
        assert!(response.ends_with("\r\n\r\n"), "{response}");
        if method == "OPTIONS" {
            assert!(response.contains("allow: GET, HEAD, OPTIONS\r\n"));
        }
    }
    drop(task);
}

#[tokio::test]
async fn diagnostics_survive_server_and_report_rejections_and_task_failures() {
    let server = Server::bind(
        loopback(),
        Limits {
            max_request_body_bytes: 1,
            max_response_body_bytes: 1,
            handler_timeout: Duration::from_millis(10),
            ..Limits::default()
        },
    )
    .await
    .unwrap();
    let addr = server.local_addr().unwrap();
    let diagnostics = server.diagnostics();
    let task = async_engine::launch(server.serve(|request| async move {
        match request.target() {
            "/panic" => panic!("test handler panic"),
            "/timeout" => std::future::pending().await,
            _ => Response::new(200, b"too large".to_vec()).unwrap(),
        }
    }));
    for path in ["/response", "/timeout", "/panic"] {
        let request =
            format!("GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n");
        exchange(addr, request.as_bytes()).await;
    }
    exchange(
        addr,
        b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nConnection: close\r\n\r\naa",
    )
    .await;
    async_engine::timeout(Duration::from_secs(2), async {
        while diagnostics.snapshot().task_failures != 1 {
            async_engine::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    task.cancel();
    let _ = task.await;
    let snapshot = diagnostics.snapshot();
    assert_eq!(snapshot.accepted_connections, 4);
    assert_eq!(snapshot.response_rejections, 1);
    assert_eq!(snapshot.handler_timeouts, 1);
    assert_eq!(snapshot.request_rejections, 1);
    assert_eq!(snapshot.task_failures, 1);
}

#[test]
fn bodyless_statuses_reject_payloads_and_connection_headers_are_private() {
    for status in [204, 205, 304] {
        assert!(Response::new(status, b"unexpected".to_vec()).is_err());
        assert!(Response::new(status, Vec::new()).is_ok());
    }
    for header in ["keep-alive", "proxy-connection", "te"] {
        assert!(Response::new(200, Vec::new())
            .unwrap()
            .with_header(header, "value")
            .is_err());
    }
}

#[tokio::test]
async fn diagnostics_report_protocol_body_and_absolute_deadlines() {
    let server = Server::bind(
        loopback(),
        Limits {
            body_timeout: Duration::from_millis(10),
            connection_timeout: Duration::from_millis(50),
            ..Limits::default()
        },
    )
    .await
    .unwrap();
    let addr = server.local_addr().unwrap();
    let diagnostics = server.diagnostics();
    let task =
        async_engine::launch(server.serve(|_| async { Response::new(200, Vec::new()).unwrap() }));
    exchange(addr, b"INVALID REQUEST\r\n\r\n").await;
    exchange(
        addr,
        b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1\r\nConnection: close\r\n\r\n",
    )
    .await;
    exchange(addr, b"GET / HTTP/1.1\r\n").await;
    async_engine::timeout(Duration::from_secs(2), async {
        loop {
            let s = diagnostics.snapshot();
            if s.connection_errors >= 1 && s.connection_timeouts == 1 && s.body_timeouts == 1 {
                break;
            }
            async_engine::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    drop(task);
}

#[tokio::test]
async fn response_header_acceptance_counts_duplicates_and_wire_bytes() {
    let server = Server::bind(
        loopback(),
        Limits {
            max_response_headers: 2,
            max_response_header_bytes: 16,
            ..Limits::default()
        },
    )
    .await
    .unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(|request| async move {
        let response = Response::new(200, Vec::new()).unwrap();
        match request.target() {
            "/count" => response
                .with_header("x", "")
                .unwrap()
                .with_header("x", "")
                .unwrap()
                .with_header("x", "")
                .unwrap(),
            "/bytes" => response.with_header("x", "123456789012").unwrap(),
            _ => response.with_header("x", "12345678901").unwrap(),
        }
    }));
    for (path, status) in [("/count", 500), ("/bytes", 500), ("/exact", 200)] {
        let request =
            format!("GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n");
        let response = exchange(addr, request.as_bytes()).await;
        assert!(
            response.starts_with(&format!("HTTP/1.1 {status}")),
            "{response}"
        );
    }
    drop(task);
}

async fn exchange(addr: SocketAddr, request: &[u8]) -> String {
    let mut socket = tokio::net::TcpStream::connect(addr).await.unwrap();
    socket.write_all(request).await.unwrap();
    let mut response = Vec::new();
    async_engine::timeout(Duration::from_secs(2), socket.read_to_end(&mut response))
        .await
        .unwrap()
        .unwrap();
    String::from_utf8(response).unwrap()
}

#[tokio::test]
async fn owned_request_response_preserves_method_target_headers_and_body() {
    let server = Server::bind(loopback(), Limits::default()).await.unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(|request| async move {
        assert_eq!(request.method(), "POST");
        assert_eq!(request.target(), "/echo?x=1");
        assert_eq!(request.header("x-test"), Some(b"value".as_slice()));
        Response::new(201, request.body().to_vec()).unwrap()
    }));
    let response = exchange(addr, b"POST /echo?x=1 HTTP/1.1\r\nHost: localhost\r\nX-Test: value\r\nContent-Length: 4\r\nConnection: close\r\n\r\nping").await;
    assert!(response.starts_with("HTTP/1.1 201"), "{response}");
    assert!(response.ends_with("ping"), "{response}");
    drop(task);
}

#[tokio::test]
async fn oversized_request_is_rejected_before_dispatch() {
    let server = Server::bind(
        loopback(),
        Limits {
            max_request_body_bytes: 3,
            ..Limits::default()
        },
    )
    .await
    .unwrap();
    let addr = server.local_addr().unwrap();
    let task =
        async_engine::launch(server.serve(|_| async { panic!("oversized body reached handler") }));
    let response = exchange(
        addr,
        b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\nConnection: close\r\n\r\nping",
    )
    .await;
    assert!(response.starts_with("HTTP/1.1 413"), "{response}");
    drop(task);
}

#[tokio::test]
async fn cancelling_server_closes_listener_and_incomplete_connections() {
    let server = Server::bind(loopback(), Limits::default()).await.unwrap();
    let addr = server.local_addr().unwrap();
    let task =
        async_engine::launch(server.serve(|_| async { Response::new(200, Vec::new()).unwrap() }));
    let mut socket = tokio::net::TcpStream::connect(addr).await.unwrap();
    socket.write_all(b"GET / HTTP/1.1\r\n").await.unwrap();
    async_engine::sleep(Duration::from_millis(20)).await;
    task.cancel();
    assert!(task.await.unwrap_err().is_cancelled());
    let mut byte = [0];
    let read = async_engine::timeout(Duration::from_secs(2), socket.read(&mut byte))
        .await
        .unwrap();
    assert!(matches!(read, Ok(0) | Err(_)));
    assert!(tokio::net::TcpStream::connect(addr).await.is_err());
}

#[tokio::test]
async fn invalid_limits_fail_before_binding() {
    assert!(Server::bind(
        loopback(),
        Limits {
            max_connections: 0,
            ..Limits::default()
        }
    )
    .await
    .is_err());
    assert!(Server::bind(
        loopback(),
        Limits {
            header_timeout: Duration::ZERO,
            ..Limits::default()
        }
    )
    .await
    .is_err());
    assert!(Response::new(99, Vec::new()).is_err());
}

#[tokio::test]
async fn connection_capacity_defers_acceptance_until_an_owned_connection_finishes() {
    let server = Server::bind(
        loopback(),
        Limits {
            max_connections: 1,
            ..Limits::default()
        },
    )
    .await
    .unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(
        server.serve(|_| async { Response::new(200, b"ok".to_vec()).unwrap() }),
    );
    let mut first = tokio::net::TcpStream::connect(addr).await.unwrap();
    first.write_all(b"GET / HTTP/1.1\r\n").await.unwrap();
    let mut second = tokio::net::TcpStream::connect(addr).await.unwrap();
    second
        .write_all(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .await
        .unwrap();
    let mut byte = [0];
    assert!(
        async_engine::timeout(Duration::from_millis(30), second.read(&mut byte))
            .await
            .is_err()
    );
    drop(first);
    let mut response = String::new();
    async_engine::timeout(Duration::from_secs(2), second.read_to_string(&mut response))
        .await
        .unwrap()
        .unwrap();
    assert!(response.ends_with("ok"), "{response}");
    drop(task);
}

#[tokio::test]
async fn body_and_handler_deadlines_return_explicit_statuses() {
    let server = Server::bind(
        loopback(),
        Limits {
            body_timeout: Duration::from_millis(30),
            handler_timeout: Duration::from_millis(30),
            ..Limits::default()
        },
    )
    .await
    .unwrap();
    let addr = server.local_addr().unwrap();
    let task =
        async_engine::launch(server.serve(|_| async { std::future::pending::<Response>().await }));
    let response = exchange(
        addr,
        b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\nConnection: close\r\n\r\np",
    )
    .await;
    assert!(response.starts_with("HTTP/1.1 408"), "{response}");
    let response = exchange(
        addr,
        b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(response.starts_with("HTTP/1.1 504"), "{response}");
    drop(task);
}

#[tokio::test]
async fn header_and_connection_deadlines_close_stalled_clients() {
    for header in [true, false] {
        let mut limits = Limits::default();
        if header {
            limits.header_timeout = Duration::from_millis(30);
        } else {
            limits.connection_timeout = Duration::from_millis(30);
        }
        let server = Server::bind(loopback(), limits).await.unwrap();
        let addr = server.local_addr().unwrap();
        let task = async_engine::launch(
            server.serve(|_| async { panic!("incomplete request dispatched") }),
        );
        let response = exchange(addr, b"GET / HTTP/1.1\r\n").await;
        assert!(
            response.is_empty() || response.starts_with("HTTP/1.1 408"),
            "{response}"
        );
        drop(task);
    }
}

#[tokio::test]
async fn response_limits_and_transport_owned_headers_are_enforced() {
    for header in [
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "trailer",
    ] {
        assert!(Response::new(200, Vec::new())
            .unwrap()
            .with_header(header, "x")
            .is_err());
    }
    assert!(Response::new(200, Vec::new())
        .unwrap()
        .with_header("x-test", "injected\r\nX: y")
        .is_err());
    let server = Server::bind(
        loopback(),
        Limits {
            max_response_body_bytes: 1,
            ..Limits::default()
        },
    )
    .await
    .unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(
        server.serve(|_| async { Response::new(200, b"too long".to_vec()).unwrap() }),
    );
    let response = exchange(
        addr,
        b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(response.starts_with("HTTP/1.1 500"), "{response}");
    drop(task);
}

#[tokio::test]
async fn file_response_streams_prefix_and_file_beyond_the_memory_body_limit() {
    use std::io::Write;
    let mut file = tempfile::NamedTempFile::new().unwrap();
    file.write_all(&vec![b'x'; 256 * 1024]).unwrap();
    let path = file.path().to_path_buf();
    let server = Server::bind(
        loopback(),
        Limits {
            max_response_body_bytes: 8,
            ..Limits::default()
        },
    )
    .await
    .unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(move |_| {
        let file = std::fs::File::open(&path).unwrap();
        async move { Response::file(file, b"PREFIX".to_vec()).unwrap() }
    }));
    let response = exchange(
        addr,
        b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    let (_, body) = response.split_once("\r\n\r\n").unwrap();
    assert_eq!(body.len(), 256 * 1024 + 6);
    assert!(body.starts_with("PREFIX"));
    assert!(body[6..].bytes().all(|byte| byte == b'x'));
    drop(task);
}

#[tokio::test]
async fn a_client_that_stops_reading_releases_its_connection_slot() {
    let file = tempfile::NamedTempFile::new().unwrap();
    file.as_file().set_len(128 * 1024 * 1024).unwrap();
    let path = file.path().to_path_buf();
    let server = Server::bind(
        loopback(),
        Limits {
            max_connections: 1,
            write_timeout: Duration::from_millis(30),
            ..Limits::default()
        },
    )
    .await
    .unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(move |request| {
        let path = path.clone();
        async move {
            if request.target() == "/file" {
                Response::file(std::fs::File::open(path).unwrap(), Vec::new()).unwrap()
            } else {
                Response::new(200, b"ok".to_vec()).unwrap()
            }
        }
    }));
    let mut stalled = tokio::net::TcpStream::connect(addr).await.unwrap();
    stalled
        .write_all(b"GET /file HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .await
        .unwrap();
    let response = exchange(
        addr,
        b"GET /ping HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(response.ends_with("ok"), "{response}");
    drop(stalled);
    drop(task);
}

#[cfg(feature = "event-stream")]
#[tokio::test]
async fn sse_response_delivers_before_source_closure_and_encodes_multiline_data() {
    let (sender, receiver) = async_engine::broadcast_channel::<String>(4).unwrap();
    let receiver = std::sync::Arc::new(std::sync::Mutex::new(Some(receiver)));
    let server = Server::bind(loopback(), Limits::default()).await.unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(move |_| {
        let receiver = receiver.lock().unwrap().take().unwrap();
        async move {
            Response::event_stream(
                receiver.into_stream_with(|result| result.ok().map(Ok)),
                Duration::from_secs(1),
            )
            .unwrap()
        }
    }));
    let mut socket = tokio::net::TcpStream::connect(addr).await.unwrap();
    socket
        .write_all(b"GET /events HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .await
        .unwrap();
    sender.send("first\nsecond".into()).unwrap();
    let mut received = String::new();
    async_engine::timeout(Duration::from_secs(2), async {
        while !received.contains("data: first\ndata: second\n\n") {
            let mut buffer = [0; 1024];
            let count = socket.read(&mut buffer).await.unwrap();
            assert!(count > 0);
            received.push_str(std::str::from_utf8(&buffer[..count]).unwrap());
        }
    })
    .await
    .unwrap();
    assert!(received.contains("text/event-stream"));
    drop(sender);
    async_engine::timeout(Duration::from_secs(2), socket.read_to_string(&mut received))
        .await
        .unwrap()
        .unwrap();
    drop(task);
}

#[cfg(feature = "event-stream")]
#[tokio::test]
async fn sse_response_emits_native_id_and_event_fields() {
    use kernal_api::http_server::SseEvent;

    let (sender, receiver) = async_engine::broadcast_channel::<SseEvent>(4).unwrap();
    let receiver = std::sync::Arc::new(std::sync::Mutex::new(Some(receiver)));
    let server = Server::bind(loopback(), Limits::default()).await.unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(move |_| {
        let receiver = receiver.lock().unwrap().take().unwrap();
        async move {
            Response::sse_stream(
                receiver.into_stream_with(|result| result.ok().map(Ok)),
                Duration::from_secs(1),
            )
            .unwrap()
        }
    }));
    let mut socket = tokio::net::TcpStream::connect(addr).await.unwrap();
    socket
        .write_all(b"GET /events HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .await
        .unwrap();
    sender
        .send(SseEvent {
            id: Some("1042".into()),
            event: Some("stdout".into()),
            data: "first\nsecond".into(),
        })
        .unwrap();
    let mut received = String::new();
    async_engine::timeout(Duration::from_secs(2), async {
        while !received.contains("id: 1042\nevent: stdout\ndata: first\ndata: second\n\n") {
            let mut buffer = [0; 1024];
            let count = socket.read(&mut buffer).await.unwrap();
            assert!(count > 0);
            received.push_str(std::str::from_utf8(&buffer[..count]).unwrap());
        }
    })
    .await
    .unwrap();
    drop(sender);
    drop(task);
}

#[tokio::test]
async fn streamed_upload_starts_before_eof_and_enforces_total_limit() {
    let server = Server::bind(
        loopback(),
        Limits {
            max_request_body_bytes: 5,
            ..Limits::default()
        },
    )
    .await
    .unwrap()
    .with_streaming_requests();
    let addr = server.local_addr().unwrap();
    let (received, mut chunks) = tokio::sync::mpsc::channel(1);
    let task = async_engine::launch(server.serve(move |mut request| {
        let received = received.clone();
        async move {
            assert!(request.body().is_empty());
            let mut first = Vec::new();
            while first.len() < 3 {
                first.extend(request.next_chunk().await.unwrap().unwrap());
            }
            received.send(first).await.unwrap();
            let error = request.next_chunk().await.unwrap_err();
            assert_eq!(error.kind(), std::io::ErrorKind::FileTooLarge);
            assert_eq!(request.next_chunk().await.unwrap_err().kind(), error.kind());
            Response::new(413, Vec::new()).unwrap()
        }
    }));
    let mut peer = tokio::net::TcpStream::connect(addr).await.unwrap();
    peer.write_all(
        b"POST /upload HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\nabc\r\n",
    )
    .await
    .unwrap();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(2), chunks.recv())
            .await
            .unwrap()
            .unwrap(),
        b"abc"
    );
    peer.write_all(b"3\r\ndef\r\n0\r\n\r\n").await.unwrap();
    let mut response = String::new();
    peer.read_to_string(&mut response).await.unwrap();
    assert!(response.starts_with("HTTP/1.1 413"), "{response}");
    drop(task);
}

#[tokio::test]
async fn stream_transport_delivers_response_chunks_before_source_closes() {
    let (mut peer, socket) = tokio::io::duplex(4096);
    let (sender, receiver) = tokio::sync::mpsc::channel(1);
    let receiver = std::sync::Mutex::new(Some(receiver));
    let task = tokio::spawn(kernal_api::http_server::serve_connection(
        socket,
        Limits::default(),
        move |_| {
            let receiver = receiver.lock().unwrap().take().unwrap();
            async move {
                Response::stream(200, tokio_stream::wrappers::ReceiverStream::new(receiver))
                    .unwrap()
            }
        },
    ));
    peer.write_all(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        .await
        .unwrap();
    sender.send(Ok(b"first".to_vec())).await.unwrap();
    let mut peer = tokio::io::BufReader::new(peer);
    use tokio::io::AsyncBufReadExt as _;
    tokio::time::timeout(Duration::from_secs(2), async {
        let mut line = String::new();
        peer.read_line(&mut line).await.unwrap();
        assert!(line.starts_with("HTTP/1.1 200"));
        while line != "\r\n" {
            line.clear();
            assert_ne!(peer.read_line(&mut line).await.unwrap(), 0);
        }
        line.clear();
        peer.read_line(&mut line).await.unwrap();
        assert_eq!(line, "5\r\n");
        let mut first = [0; 7];
        peer.read_exact(&mut first).await.unwrap();
        assert_eq!(&first, b"first\r\n");
    })
    .await
    .unwrap();
    sender.send(Ok(b"second".to_vec())).await.unwrap();
    drop(sender);
    let mut rest = String::new();
    peer.read_to_string(&mut rest).await.unwrap();
    assert_eq!(rest, "6\r\nsecond\r\n0\r\n\r\n");
    task.await.unwrap().unwrap();
}

#[tokio::test]
async fn route_matches_method_and_captures_encoded_segments() {
    let server = Server::bind(loopback(), Limits::default()).await.unwrap();
    let addr = server.local_addr().unwrap();
    let task = async_engine::launch(server.serve(|request| async move {
        assert!(request
            .match_route("POST", "/v1/:id/artifact/:name")
            .is_none());
        assert!(request
            .match_route("GET", "/v1/:id/artifact/:name/")
            .is_none());
        assert!(request.match_route("GET", "/v1/:id/artifact").is_none());
        let params = request
            .match_route("GET", "/v1/:id/artifact/:name")
            .unwrap();
        assert_eq!(params.get("id"), Some("a%2Fb"));
        assert_eq!(params.get("name"), Some("out+log"));
        assert_eq!(params.get("other"), None);
        Response::new(204, Vec::new()).unwrap()
    }));
    let response = exchange(
        addr,
        b"GET /v1/a%2Fb/artifact/out+log?download=1 HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
    )
    .await;
    assert!(response.starts_with("HTTP/1.1 204"), "{response}");
    drop(task);
}
