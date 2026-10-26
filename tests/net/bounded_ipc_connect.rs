#![cfg(feature = "ipc")]
//! `platform::ipc::Stream` offers a connect bounded by a timeout.

use std::io::{self, Read, Write};
use std::time::{Duration, Instant};

use kernal_api::platform::ipc::{Endpoint, Listener, Stream};

#[test]
fn a_bounded_connect_reaches_a_live_listener() {
    let endpoint = Endpoint::test("bounded-connect").unwrap();
    let listener = Listener::bind(&endpoint).unwrap();
    let server = std::thread::spawn(move || {
        let mut stream = listener.accept().unwrap();
        let mut byte = [0_u8; 1];
        stream.read_exact(&mut byte).unwrap();
        byte[0]
    });
    let mut client = Stream::connect_within(&endpoint, Duration::from_secs(30)).unwrap();
    client.write_all(&[7]).unwrap();
    assert_eq!(server.join().unwrap(), 7);
}

#[test]
fn a_bounded_connect_to_a_missing_endpoint_fails_without_waiting() {
    let endpoint = Endpoint::test("bounded-connect-missing").unwrap();
    let started = Instant::now();
    let error = Stream::connect_within(&endpoint, Duration::from_secs(30))
        .expect_err("nothing listens on a fresh endpoint");
    assert_ne!(error.kind(), io::ErrorKind::TimedOut, "{error}");
    assert!(started.elapsed() < Duration::from_secs(10));
}
