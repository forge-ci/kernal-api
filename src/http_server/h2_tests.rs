use super::*;
use crate::http::{Client, Request as ClientRequest, Version};
use hyper_util::rt::TokioExecutor;
use tokio_rustls::rustls::{
    pki_types::{pem::PemObject, CertificateDer, PrivateKeyDer},
    ServerConfig,
};

const ROOT: &[u8] = include_bytes!("../../tests/fixtures/http-tls/chain-root.pem");
const IDENTITY: &[u8] = include_bytes!("../../tests/fixtures/http-tls/chain-leaf.pem");

fn tls_acceptor(alpn_protocols: Vec<Vec<u8>>) -> tokio_rustls::TlsAcceptor {
    let mut tls = ServerConfig::builder_with_provider(Arc::new(
        tokio_rustls::rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .unwrap()
    .with_no_client_auth()
    .with_single_cert(
        vec![CertificateDer::from_pem_slice(IDENTITY).unwrap()],
        PrivateKeyDer::from_pem_slice(IDENTITY).unwrap(),
    )
    .unwrap();
    tls.alpn_protocols = alpn_protocols;
    tokio_rustls::TlsAcceptor::from(Arc::new(tls))
}

async fn serve_h2<H, F>(
    server: Server,
    acceptor: tokio_rustls::TlsAcceptor,
    handler: H,
) -> Result<(), hyper::Error>
where
    H: Fn(Request) -> F + Clone + Send + Sync + 'static,
    F: Future<Output = Response> + Send + 'static,
{
    // Accept exactly one socket and route every HTTP/2 stream through the
    // same server dispatch used by Server::serve.
    let (socket, _) = server.listener.accept().await.unwrap();
    let socket = acceptor.accept(socket).await.unwrap();
    assert_eq!(socket.get_ref().1.alpn_protocol(), Some(b"h2".as_slice()));
    #[cfg(feature = "websocket")]
    let (upgrades, _receiver) = tokio::sync::mpsc::unbounded_channel();
    hyper::server::conn::http2::Builder::new(TokioExecutor::new())
        .serve_connection(
            TokioIo::new(socket),
            service_fn(move |request| {
                dispatch(
                    request,
                    handler.clone(),
                    server.limits,
                    server.diagnostics(),
                    server.response_headers.clone(),
                    server.read_budget.clone(),
                    #[cfg(feature = "websocket")]
                    upgrades.clone(),
                )
            }),
        )
        .await
}

#[tokio::test]
async fn tls_negotiates_h2_and_reuses_it_while_streaming() {
    let acceptor = tls_acceptor(vec![b"h2".to_vec(), b"http/1.1".to_vec()]);
    let server = Server::bind("127.0.0.1:0".parse().unwrap(), Limits::default())
        .await
        .unwrap();
    let base = format!("https://localhost:{}", server.local_addr().unwrap().port());
    let (events, receiver) = tokio::sync::mpsc::channel(2);
    let receiver = Arc::new(std::sync::Mutex::new(Some(receiver)));
    let handler = move |request: Request| {
        let response = match request.path() {
            "/events" => Response::event_stream(
                tokio_stream::wrappers::ReceiverStream::new(
                    receiver.lock().unwrap().take().unwrap(),
                ),
                Duration::from_secs(60),
            )
            .unwrap(),
            "/redirect" => Response::new(302, Vec::new())
                .unwrap()
                .with_header("location", "/range")
                .unwrap(),
            "/range" => {
                assert_eq!(request.header("range"), Some(b"bytes=2-4".as_slice()));
                Response::new(206, b"cde".to_vec())
                    .unwrap()
                    .with_header("content-range", "bytes 2-4/6")
                    .unwrap()
            }
            other => panic!("unexpected path {other}"),
        };
        std::future::ready(response)
    };
    let peer = tokio::spawn(serve_h2(server, acceptor, handler));

    let client = Client::with_test_root(
        crate::http::Limits {
            max_redirects: 1,
            total_timeout: Duration::from_secs(10),
            ..crate::http::Limits::default()
        },
        ROOT,
    )
    .unwrap();
    let url = format!("{base}/events");
    events.send(Ok("first".into())).await.unwrap();
    let mut response = client.get(&url).await.unwrap();
    assert_eq!(response.version(), Version::Http2);
    assert_eq!(
        response.next_chunk().await.unwrap().unwrap().as_ref(),
        b"data: first\n\n"
    );

    let url = format!("{base}/redirect");
    let ranged = client
        .clone()
        .execute(ClientRequest {
            headers: &[("range", "bytes=2-4")],
            ..ClientRequest::get(&url)
        })
        .await
        .unwrap();
    assert_eq!(ranged.version(), Version::Http2);
    assert_eq!(ranged.status(), 206);
    assert_eq!(
        ranged.header("Content-Range"),
        Some(b"bytes 2-4/6".as_slice())
    );
    assert_eq!(ranged.into_bytes().await.unwrap(), b"cde");

    events.send(Ok("second".into())).await.unwrap();
    drop(events);
    let mut prefix = [0; 6];
    assert_eq!(response.read(&mut prefix).await.unwrap(), prefix.len());
    assert_eq!(&prefix, b"data: ");
    assert_eq!(
        response.next_chunk().await.unwrap().unwrap().as_ref(),
        b"second\n\n"
    );
    assert!(response.next_chunk().await.unwrap().is_none());
    peer.abort();
}

#[cfg(all(feature = "websocket", feature = "websocket-client"))]
#[tokio::test]
async fn websocket_tls_uses_http1_when_peer_also_offers_h2() {
    use crate::http::websocket::{connect_with_root, Message, WebSocketLimits};
    use tokio::net::{TcpListener, TcpStream};

    let server = Server::bind("127.0.0.1:0".parse().unwrap(), Limits::default())
        .await
        .unwrap();
    let server_addr = server.local_addr().unwrap();
    let server_task = tokio::spawn(server.serve(|request: Request| async move {
        assert_eq!(
            request.header("authorization"),
            Some(b"Bearer fixture".as_slice())
        );
        request
            .into_websocket()
            .unwrap()
            .on_upgrade(WebSocketLimits::default(), |mut socket| async move {
                socket.send(Message::Binary(vec![1, 2, 3])).await.unwrap();
            })
            .unwrap()
    }));

    // Terminate TLS in front of the crate's plain HTTP/1.1 server.
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!(
        "wss://localhost:{}/socket",
        listener.local_addr().unwrap().port()
    );
    let proxy = tokio::spawn(async move {
        let acceptor = tls_acceptor(vec![b"h2".to_vec(), b"http/1.1".to_vec()]);
        let (socket, _) = listener.accept().await.unwrap();
        let mut tls = acceptor.accept(socket).await.unwrap();
        assert_eq!(
            tls.get_ref().1.alpn_protocol(),
            Some(b"http/1.1".as_slice())
        );
        let mut upstream = TcpStream::connect(server_addr).await.unwrap();
        tokio::io::copy_bidirectional(&mut tls, &mut upstream)
            .await
            .unwrap();
    });

    tokio::time::timeout(Duration::from_secs(5), async {
        let mut socket = connect_with_root(
            &url,
            &[("Authorization", "Bearer fixture")],
            WebSocketLimits::default(),
            Some(ROOT),
        )
        .await
        .unwrap();
        assert_eq!(
            socket.receive().await.unwrap(),
            Some(Message::Binary(vec![1, 2, 3]))
        );
    })
    .await
    .unwrap();
    proxy.abort();
    server_task.abort();
}
