//! Which environment variable holds the API key for a configured endpoint (SPEC §11.1).

pub const VENDOR_KEY_ENVS: &[&str] = &["OPENAI_API_KEY", "ANTHROPIC_API_KEY"];
const URL_SUFFIXES: &[&str] = &["_BASE_URL", "_URL"];

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
