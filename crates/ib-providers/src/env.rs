//! Which environment variable holds the API key for a configured endpoint (SPEC §11.1).

use std::collections::BTreeMap;
use std::ffi::OsString;

pub const VENDOR_KEY_ENVS: &[&str] = &["OPENAI_API_KEY", "ANTHROPIC_API_KEY"];
const URL_SUFFIXES: &[&str] = &["_BASE_URL", "_URL"];

/// Snapshot the process environment without panicking on non-Unicode OS strings.
///
/// Environment configuration is advisory; entries that cannot be represented as UTF-8 are
/// ignored because provider configs and child commands use UTF-8 keys and values.
#[must_use]
pub fn unicode_env_snapshot() -> BTreeMap<String, String> {
    unicode_env_snapshot_from(std::env::vars_os())
}

fn unicode_env_snapshot_from(
    vars: impl IntoIterator<Item = (OsString, OsString)>,
) -> BTreeMap<String, String> {
    vars.into_iter()
        .filter_map(|(name, value)| Some((name.into_string().ok()?, value.into_string().ok()?)))
        .collect()
}

/// The key variable conventionally paired with a base-URL variable.
///
/// `LOCAL_LLM_BASE_URL` pairs with `LOCAL_LLM_API_KEY` and `GATEWAY_URL` with `GATEWAY_API_KEY`.
/// A pairing that lands on a vendor key (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`) returns `None`.
#[must_use]
pub fn paired_api_key_env(base_url_env: &str) -> Option<String> {
    for suffix in URL_SUFFIXES {
        if let Some(prefix) = base_url_env.strip_suffix(suffix)
            && !prefix.is_empty()
        {
            let paired = format!("{prefix}_API_KEY");
            if VENDOR_KEY_ENVS.contains(&paired.as_str()) {
                return None;
            }
            return Some(paired);
        }
    }
    None
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::ffi::OsStringExt as _;

    #[test]
    fn process_environment_snapshot_skips_non_unicode_pairs() {
        let vars = [
            (OsString::from("VALID"), OsString::from("value")),
            (
                OsString::from_vec(b"BAD\xffKEY".to_vec()),
                OsString::from("ignored-key"),
            ),
            (
                OsString::from("BAD_VALUE"),
                OsString::from_vec(b"ignored\xffvalue".to_vec()),
            ),
        ];

        assert_eq!(
            unicode_env_snapshot_from(vars),
            BTreeMap::from([("VALID".to_string(), "value".to_string())])
        );
    }
}
