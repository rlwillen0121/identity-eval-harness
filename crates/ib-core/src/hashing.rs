//! Hash helpers mirroring the `hashlib` call sites in the Python harness.

use sha2::{Digest, Sha256};

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
