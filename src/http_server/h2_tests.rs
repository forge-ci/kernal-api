use super::*;
use crate::http::{Client, Request as ClientRequest, Version};
use tokio_rustls::rustls::{
    pki_types::{pem::PemObject, CertificateDer, PrivateKeyDer},
    ServerConfig,
};

const ROOT: &[u8] = include_bytes!("../../tests/fixtures/http-tls/chain-root.pem");
const IDENTITY: &[u8] = include_bytes!("../../tests/fixtures/http-tls/chain-leaf.pem");

fn h2_acceptor() -> tokio_rustls::TlsAcceptor {
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
    tls.alpn_protocols = vec![b"h2".to_vec(), b"http/1.1".to_vec()];
    tokio_rustls::TlsAcceptor::from(Arc::new(tls))
}

async fn serve_h2<H, F>(
    server: Server,
    acceptor: tokio_rustls::TlsAcceptor,
    handler: H,
) -> io::Result<()>
where
    H: Fn(Request) -> F + Clone + Send + Sync + 'static,
    F: Future<Output = Response> + Send + 'static,
{
    let (socket, _) = server.listener.accept().await.unwrap();
    let socket = acceptor.accept(socket).await.unwrap();
    assert_eq!(socket.get_ref().1.alpn_protocol(), Some(b"h2".as_slice()));
    serve_connection(socket, server.limits, handler).await
}

#[tokio::test]
async fn tls_negotiates_h2_and_reuses_it_while_streaming() {
    let acceptor = h2_acceptor();
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

#[tokio::test]
async fn prior_knowledge_and_http1_share_a_listener() {
    use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};

    tokio::time::timeout(Duration::from_secs(5), async {
        let server = Server::bind("127.0.0.1:0".parse().unwrap(), Limits::default())
            .await
            .unwrap();
        let addr = server.local_addr().unwrap();
        let server = tokio::spawn(server.serve_with(|| {
            let requests = std::sync::atomic::AtomicUsize::new(0);
            move |request| {
                let n = requests.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                let body = format!("{n}:{}", String::from_utf8_lossy(request.body()));
                std::future::ready(Response::new(200, body.into_bytes()).unwrap())
            }
        }));

        let socket = tokio::net::TcpStream::connect(addr).await.unwrap();
        let (mut client, connection) = hyper::client::conn::http2::handshake(
            hyper_util::rt::TokioExecutor::new(),
            TokioIo::new(socket),
        )
        .await
        .unwrap();
        let h2 = tokio::spawn(connection);
        for expected in [b"0:upload", b"1:upload"] {
            let request = hyper::Request::post(format!("http://{addr}/upload"))
                .body(http_body_util::Full::new(Bytes::from_static(b"upload")))
                .unwrap();
            let response = client.send_request(request).await.unwrap();
            assert_eq!(response.version(), hyper::Version::HTTP_2);
            assert_eq!(
                response.into_body().collect().await.unwrap().to_bytes(),
                &expected[..]
            );
        }

        let mut h1 = tokio::net::TcpStream::connect(addr).await.unwrap();
        h1.write_all(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            .await
            .unwrap();
        let mut response = String::new();
        h1.read_to_string(&mut response).await.unwrap();
        assert!(response.starts_with("HTTP/1.1 200"));
        assert!(response.ends_with("\r\n\r\n0:"));
        server.abort();
        h2.abort();
    })
    .await
    .unwrap();
}
