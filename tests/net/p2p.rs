#![cfg(feature = "p2p")]

use std::io;
use std::net::{Ipv4Addr, Ipv6Addr, SocketAddr};
use std::time::Duration;

use kernal_api::p2p::{Endpoint, Path, PeerId, RelayMode, SecretKey};

#[tokio::test]
async fn direct_peers_echo_over_a_bidirectional_stream() -> io::Result<()> {
    const ALPN: &[u8] = b"kernal-api/echo";
    const PAYLOAD: &[u8] = b"loopback echo";

    tokio::time::timeout(Duration::from_secs(15), async {
        let key = SecretKey::generate();
        let client = Endpoint::bind(
            SecretKey::from_bytes(&key.to_bytes()),
            None,
            RelayMode::Default,
        )
        .await?;
        assert_eq!(client.id(), key.public());
        let server = Endpoint::bind(SecretKey::generate(), None, RelayMode::Default).await?;
        server.set_alpns(&[ALPN]);
        let server_id: PeerId = server.id().to_string().parse()?;
        let socket = server
            .bound_sockets()
            .into_iter()
            .next()
            .expect("direct endpoints bind a UDP socket");
        let loopback = if socket.is_ipv4() {
            SocketAddr::from((Ipv4Addr::LOCALHOST, socket.port()))
        } else {
            SocketAddr::from((Ipv6Addr::LOCALHOST, socket.port()))
        };

        let echo = async {
            let connection = server.accept().await.expect("server is open")?;
            assert_eq!(connection.remote_id(), client.id());
            assert_eq!(connection.alpn(), ALPN);
            let (mut send, mut recv) = connection.accept_bi().await?;
            let data = recv.read_to_end(PAYLOAD.len()).await?;
            send.write_all(&data).await?;
            send.finish()?;
            connection.closed().await;
            Ok::<_, io::Error>(())
        };
        let request = async {
            let connection = client.connect_direct(server_id, loopback, ALPN).await?;
            assert_eq!(connection.remote_id(), server_id);
            assert_eq!(
                connection.observe_path().next_path().await,
                Some(Path::Direct)
            );
            assert_eq!(connection.path(), Some(Path::Direct));
            let (mut send, mut recv) = connection.open_bi().await?;
            send.write_all(PAYLOAD).await?;
            send.finish()?;
            assert_eq!(recv.read_to_end(PAYLOAD.len()).await?, PAYLOAD);
            connection.close(0, b"echo complete");
            Ok::<_, io::Error>(())
        };
        tokio::try_join!(echo, request)?;
        tokio::join!(client.close(), server.close());
        Ok(())
    })
    .await
    .expect("the loopback exchange should finish within 15 seconds")
}
