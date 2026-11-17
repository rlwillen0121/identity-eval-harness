//! Hash helpers mirroring the `hashlib` call sites in the Python harness.

use sha2::{Digest, Sha256, Sha512};

/// `hashlib.sha256(data).hexdigest()`.
#[must_use]
pub fn sha256_hex(data: impl AsRef<[u8]>) -> String {
    hex::encode(Sha256::digest(data.as_ref()))
}

/// `hashlib.sha256(data).digest()`.
#[must_use]
pub fn sha256(data: impl AsRef<[u8]>) -> [u8; 32] {
    Sha256::digest(data.as_ref()).into()
}

/// `hashlib.sha512(data).hexdigest()`.
#[must_use]
pub fn sha512_hex(data: impl AsRef<[u8]>) -> String {
    hex::encode(Sha512::digest(data.as_ref()))
}

/// `int.from_bytes(hashlib.sha256(data).digest()[:8], "big")`.
#[must_use]
pub fn sha256_prefix_u64(data: impl AsRef<[u8]>) -> u64 {
    let d = sha256(data);
    u64::from_be_bytes(d[..8].try_into().expect("8 bytes"))
}

/// Hex digest of a file's bytes.
///
/// # Errors
/// Propagates I/O errors.
pub fn sha256_file_hex(path: &std::path::Path) -> std::io::Result<String> {
    Ok(sha256_hex(std::fs::read(path)?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_vectors() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }
}
