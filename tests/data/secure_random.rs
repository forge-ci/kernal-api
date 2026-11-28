#![cfg(feature = "secure-random")]

use kernal_api::random::{RandomError, SecureRandom, MAX_RANDOM_BYTES};
use std::time::Duration;

// Issue #180: the native smoke checks availability/shape, not statistical quality.
#[tokio::test]
async fn native_entropy_obeys_the_bounded_contract() {
    let random = SecureRandom::new(1, Duration::from_secs(5)).unwrap();
    assert!(random.bytes(0).await.unwrap().is_empty());
    assert_eq!(random.bytes(32).await.unwrap().len(), 32);
    assert_eq!(
        random.bytes(MAX_RANDOM_BYTES + 1).await.unwrap_err(),
        RandomError::TooLarge
    );
}

#[test]
fn rejects_invalid_configuration() {
    assert!(SecureRandom::new(0, Duration::from_secs(1)).is_err());
    assert!(SecureRandom::new(65, Duration::from_secs(1)).is_err());
    assert!(SecureRandom::new(1, Duration::ZERO).is_err());
}

#[test]
fn sync_fill_uses_os_entropy_without_a_runtime() {
    let mut empty = [0u8; 0];
    SecureRandom::fill(&mut empty).unwrap();
    let mut first = [0u8; 32];
    let mut second = [0u8; 32];
    SecureRandom::fill(&mut first).unwrap();
    SecureRandom::fill(&mut second).unwrap();
    assert_ne!(first, second, "two 256-bit draws collided");
    let mut large = vec![0u8; MAX_RANDOM_BYTES + 1];
    SecureRandom::fill(&mut large).unwrap();
    assert!(large.iter().any(|&byte| byte != 0));
}
