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

/// `SHA-256(parts[0] || parts[1] || …)`: the digest of the concatenated byte strings.
#[must_use]
pub fn sha256_parts(parts: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    for part in parts {
        hasher.update(part);
    }
    hasher.finalize().into()
}

/// Whether every byte of `text` is a lowercase hex digit `[0-9a-f]` (no length check).
#[must_use]
pub fn is_lower_hex(text: &str) -> bool {
    text.bytes()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// Decode exactly 64 lowercase hex characters (after trimming surrounding whitespace) into 32 raw
/// bytes.
///
/// # Errors
/// Returns a description (never containing the input) if the text is not 64 lowercase hex characters.
pub fn decode_hex32(text: &str) -> Result<[u8; 32], String> {
    let trimmed = text.trim();
    if trimmed.len() != 64 {
        return Err(format!(
            "expected 64 lowercase hex characters, got {} characters",
            trimmed.len()
        ));
    }
    if !is_lower_hex(trimmed) {
        return Err("expected lowercase hex characters [0-9a-f] only".to_owned());
    }
    let mut out = [0u8; 32];
    hex::decode_to_slice(trimmed, &mut out).map_err(|e| format!("invalid hex: {e}"))?;
    Ok(out)
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
        assert_eq!(sha256_parts(&[b"a", b"", b"bc"]), sha256(b"abc"));
        assert_eq!(sha256_parts(&[]), sha256(b""));
    }

    #[test]
    fn hex32_decoding_is_strict_lowercase() {
        let lower = "00ff".repeat(16);
        let mut expected = [0u8; 32];
        for pair in expected.chunks_mut(2) {
            pair[1] = 0xff;
        }
        assert_eq!(decode_hex32(&lower), Ok(expected));
        assert_eq!(decode_hex32(&format!(" {lower}\n")), Ok(expected));
        assert!(decode_hex32(&lower.to_uppercase()).is_err());
        assert!(decode_hex32(&lower[..62]).is_err());
        assert!(decode_hex32(&format!("{}zz", &lower[..62])).is_err());
        assert!(is_lower_hex("0123456789abcdef"));
        assert!(!is_lower_hex("0123456789ABCDEF"));
        assert!(!is_lower_hex("+f"));
    }
}
