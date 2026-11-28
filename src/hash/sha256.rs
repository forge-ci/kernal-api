//! SHA-256 compatibility for artifact seals and existing cache formats.

use sha2::Digest as _;
use std::fmt;
use std::io::{self, Read};
use std::path::Path;

/// Canonical SHA-256 bytes, without implementation-crate types.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Sha256Digest([u8; 32]);

impl Sha256Digest {
    /// Borrow the fixed-width digest encoding.
    pub fn as_bytes(&self) -> &[u8; 32] {
        &self.0
    }

    /// Render lower-case hexadecimal, preserving leading zeroes.
    pub fn to_hex(self) -> String {
        let mut output = String::with_capacity(64);
        const HEX: &[u8; 16] = b"0123456789abcdef";
        for byte in self.0 {
            output.push(HEX[usize::from(byte >> 4)] as char);
            output.push(HEX[usize::from(byte & 15)] as char);
        }
        output
    }
}

impl AsRef<[u8]> for Sha256Digest {
    fn as_ref(&self) -> &[u8] {
        &self.0
    }
}

impl AsRef<[u8; 32]> for Sha256Digest {
    fn as_ref(&self) -> &[u8; 32] {
        &self.0
    }
}

impl std::borrow::Borrow<[u8]> for Sha256Digest {
    fn borrow(&self) -> &[u8] {
        &self.0
    }
}

impl std::ops::Deref for Sha256Digest {
    type Target = [u8; 32];

    fn deref(&self) -> &[u8; 32] {
        &self.0
    }
}

impl From<Sha256Digest> for [u8; 32] {
    fn from(digest: Sha256Digest) -> Self {
        digest.0
    }
}

impl From<[u8; 32]> for Sha256Digest {
    /// Wrap bytes already known to be a SHA-256 digest (for example, decoded
    /// from a stored seal). No hashing is performed.
    fn from(bytes: [u8; 32]) -> Self {
        Self(bytes)
    }
}

impl PartialEq<[u8; 32]> for Sha256Digest {
    fn eq(&self, other: &[u8; 32]) -> bool {
        self.0 == *other
    }
}

impl fmt::LowerHex for Sha256Digest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.to_hex())
    }
}

impl fmt::Display for Sha256Digest {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.to_hex())
    }
}

/// Constant-memory incremental SHA-256 state.
#[derive(Clone, Default)]
pub struct Sha256Hasher(sha2::Sha256);

impl Sha256Hasher {
    /// Start an empty message.
    pub fn new() -> Self {
        Self::default()
    }

    /// Add bytes without retaining the input.
    pub fn update(&mut self, bytes: impl AsRef<[u8]>) {
        self.0.update(bytes);
    }

    /// Finish this message and consume the state.
    pub fn finalize(self) -> Sha256Digest {
        Sha256Digest(self.0.finalize().into())
    }

    /// Hash one in-memory message.
    pub fn digest(bytes: impl AsRef<[u8]>) -> Sha256Digest {
        sha256_bytes(bytes.as_ref())
    }
}

/// Feed written bytes into the hash, so `std::io::copy(reader, &mut hasher)`
/// works. Writes always consume the whole buffer and never fail; there is no
/// byte limit, unlike [`sha256_reader`].
impl io::Write for Sha256Hasher {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0.update(bytes);
        Ok(bytes.len())
    }

    fn write_all(&mut self, bytes: &[u8]) -> io::Result<()> {
        self.0.update(bytes);
        Ok(())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// Hash one in-memory message.
pub fn sha256_bytes(bytes: &[u8]) -> Sha256Digest {
    let mut hasher = Sha256Hasher::new();
    hasher.update(bytes);
    hasher.finalize()
}

/// Hash at most `max_bytes`, using a fixed 64 KiB buffer.
///
/// Retries interrupted reads and propagates other I/O errors. Reads at most
/// one byte beyond the limit to distinguish exact-length input from overflow;
/// overflow returns `InvalidData`, never a digest of a truncated message.
pub fn sha256_reader(mut reader: impl Read, max_bytes: u64) -> io::Result<Sha256Digest> {
    let mut hasher = Sha256Hasher::new();
    let mut remaining = max_bytes;
    let mut buffer = [0; 64 * 1024];
    loop {
        let capacity = remaining.saturating_add(1).min(buffer.len() as u64) as usize;
        let count = match reader.read(&mut buffer[..capacity]) {
            Ok(0) => return Ok(hasher.finalize()),
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        if count as u64 > remaining {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "SHA-256 input exceeds byte limit",
            ));
        }
        hasher.update(&buffer[..count]);
        remaining -= count as u64;
    }
}

/// Open and stream a file under the same limit as [`sha256_reader`].
pub fn sha256_file(path: impl AsRef<Path>, max_bytes: u64) -> io::Result<Sha256Digest> {
    sha256_reader(std::fs::File::open(path)?, max_bytes)
}
