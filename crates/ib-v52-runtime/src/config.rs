//! YAML run configuration, pricing rate loader, and dataset loader for IdentityBench v5.2.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use ib_accounting::{FrozenPricingRate, HasUsageTokenCounts, estimate_frozen_cost_micros};
use ib_core::hashing::sha256_hex;
use ib_providers::factory::{ModelSpec, ProviderKind};
use ib_v52_schemas::{EpisodeV52, SealedEpisodeOracleV52};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::protocol::PER_CALL_TIMEOUT_S_V52;

/// Pricing rates in USD micros per 1M tokens.
///
/// Cache-write tokens are priced at `cache_write_micros_per_million` when present and are never
/// folded into cache-read tokens (`SPEC-v5.2` §10.2). Without a cache-write rate they are priced
/// as uncached input.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct PricingRatesV52 {
    pub uncached_input_micros_per_million: i64,
    pub cached_input_micros_per_million: i64,
    #[serde(default)]
    pub cache_write_micros_per_million: Option<i64>,
    pub output_micros_per_million: i64,
    pub reasoning_micros_per_million: i64,
}

impl PricingRatesV52 {
    /// Parse either micros-per-million JSON or calibration USD-per-1M JSON.
    pub fn from_json_value(value: &Value) -> Result<Self> {
        let obj = value
            .as_object()
            .ok_or_else(|| anyhow::anyhow!("pricing JSON must be an object"))?;

        if obj.contains_key("uncached_input_micros_per_million") {
            let get_micros = |key: &str| -> Result<i64> {
                let n = obj
                    .get(key)
                    .and_then(Value::as_i64)
                    .ok_or_else(|| anyhow::anyhow!("pricing field '{key}' must be an integer"))?;
                if n < 0 {
                    bail!("pricing field '{key}' must be non-negative");
                }
                Ok(n)
            };
            let cache_write = match obj.get("cache_write_micros_per_million") {
                None | Some(Value::Null) => None,
                Some(_) => Some(get_micros("cache_write_micros_per_million")?),
            };
            return Ok(Self {
                uncached_input_micros_per_million: get_micros("uncached_input_micros_per_million")?,
                cached_input_micros_per_million: get_micros("cached_input_micros_per_million")?,
                cache_write_micros_per_million: cache_write,
                output_micros_per_million: get_micros("output_micros_per_million")?,
                reasoning_micros_per_million: get_micros("reasoning_micros_per_million")?,
            });
        }

        if obj.contains_key("input_per_1m_usd") {
            let get_usd_micros = |key: &str| -> Result<i64> {
                let f = obj
                    .get(key)
                    .and_then(Value::as_f64)
                    .ok_or_else(|| anyhow::anyhow!("pricing field '{key}' must be a number"))?;
                if !f.is_finite() || f < 0.0 {
                    bail!("pricing field '{key}' must be finite and non-negative");
                }
                Ok((f * 1_000_000.0).round() as i64)
            };
            let uncached = get_usd_micros("input_per_1m_usd")?;
            let cached = match obj.get("cached_input_per_1m_usd") {
                None | Some(Value::Null) => 0,
                Some(_) => get_usd_micros("cached_input_per_1m_usd")?,
            };
            let cache_write = match obj.get("cache_write_per_1m_usd") {
                None | Some(Value::Null) => None,
                Some(_) => Some(get_usd_micros("cache_write_per_1m_usd")?),
            };
            let output = get_usd_micros("output_per_1m_usd")?;
            let reasoning = match obj.get("reasoning_per_1m_usd") {
                None | Some(Value::Null) => output,
                Some(_) => get_usd_micros("reasoning_per_1m_usd")?,
            };
            return Ok(Self {
                uncached_input_micros_per_million: uncached,
                cached_input_micros_per_million: cached,
                cache_write_micros_per_million: cache_write,
                output_micros_per_million: output,
                reasoning_micros_per_million: reasoning,
            });
        }

        bail!(
            "unrecognized pricing JSON schema; expected uncached_input_micros_per_million or input_per_1m_usd"
        )
    }

    /// Convert to `ib-accounting`'s `FrozenPricingRate` representation.
    #[must_use]
    pub fn to_frozen_rate(&self, pricing_manifest_sha256: &str) -> FrozenPricingRate {
        FrozenPricingRate {
            pricing_manifest_sha256: pricing_manifest_sha256.to_owned(),
            currency_code: "USD".to_owned(),
            input_cost_per_million_micros: self.uncached_input_micros_per_million,
            cached_input_cost_per_million_micros: Some(self.cached_input_micros_per_million),
            cache_write_cost_per_million_micros: self.cache_write_micros_per_million,
            output_cost_per_million_micros: self.output_micros_per_million,
            reasoning_cost_per_million_micros: Some(self.reasoning_micros_per_million),
        }
    }

    /// Compute rounded integer USD micros from accumulated token counts. `input_tokens` is
    /// uncached input; cache-read and cache-write tokens are separate, disjoint counts.
    #[must_use]
    pub fn compute_cost_usd_micros(
        &self,
        input_tokens: u64,
        cache_read_tokens: u64,
        cache_write_tokens: u64,
        output_tokens: u64,
        reasoning_tokens: u64,
    ) -> u64 {
        struct TokenSnapshot {
            total_in: i64,
            cached_in: i64,
            cache_write_in: i64,
            total_out: i64,
            reasoning: i64,
        }
        impl HasUsageTokenCounts for TokenSnapshot {
            fn input_tokens_total(&self) -> Option<i64> {
                Some(self.total_in)
            }
            fn cached_input_tokens(&self) -> Option<i64> {
                Some(self.cached_in)
            }
            fn output_tokens_total(&self) -> Option<i64> {
                Some(self.total_out)
            }
            fn reasoning_tokens_subset(&self) -> Option<i64> {
                Some(self.reasoning)
            }
            fn cache_write_input_tokens(&self) -> Option<i64> {
                Some(self.cache_write_in)
            }
        }
        let to_i64 = |n: u64| i64::try_from(n).unwrap_or(i64::MAX);
        let snapshot = TokenSnapshot {
            total_in: to_i64(
                input_tokens
                    .saturating_add(cache_read_tokens)
                    .saturating_add(cache_write_tokens),
            ),
            cached_in: to_i64(cache_read_tokens),
            cache_write_in: to_i64(cache_write_tokens),
            total_out: to_i64(output_tokens),
            reasoning: to_i64(reasoning_tokens.min(output_tokens)),
        };
        let frozen = self.to_frozen_rate(&"0".repeat(64));
        match estimate_frozen_cost_micros(&snapshot, &frozen) {
            Some(micros) if micros >= 0 => micros as u64,
            _ => 0,
        }
    }
}

/// Loaded pricing artifact with raw SHA-256 digest (`pricing_sha256`).
#[derive(Debug, Clone)]
pub struct LoadedPricingV52 {
    pub rates: PricingRatesV52,
    pub sha256: String,
    pub raw_bytes: Vec<u8>,
}

pub fn load_pricing_v52(path: &Path) -> Result<LoadedPricingV52> {
    let raw_bytes = std::fs::read(path)
        .with_context(|| format!("failed to read pricing {}", path.display()))?;
    let value: Value = serde_json::from_slice(&raw_bytes).context("pricing JSON is invalid")?;
    let rates = PricingRatesV52::from_json_value(&value)?;
    let sha256 = sha256_hex(&raw_bytes);
    Ok(LoadedPricingV52 {
        rates,
        sha256,
        raw_bytes,
    })
}

/// Optional `run_config` YAML section. Budget, ceiling, output-token and prompt overrides are
/// not accepted (`SPEC-v5.2` §10.1 item 5).
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct RunConfigV52 {
    pub effort: Option<String>,
    pub temperature: Option<f64>,
    /// The model's declared context window in tokens, recorded in the probe receipt (§11.2.1).
    pub context_window_tokens: Option<u64>,
}

/// Transports whose requests carry a provider output-token parameter (§7.3).
#[must_use]
pub fn is_http_transport(kind: ProviderKind) -> bool {
    kind.is_api_kind()
}

/// Complete YAML run configuration (`model`, optional `model_tier`, optional `run_config`).
#[derive(Debug, Clone)]
pub struct LoadedRunConfigV52 {
    pub model_spec: ModelSpec,
    pub run_config: RunConfigV52,
    pub model_tier: Option<String>,
    /// Reasoning effort of the entry, or `transport_default` when none is configured.
    pub effort: String,
    /// `transport_id` of the entry (the provider kind).
    pub transport_id: String,
    /// Requested output-token parameter: the model's vendor maximum for HTTP transports; `None`
    /// for transports that expose no such parameter (§7.3).
    pub requested_output_tokens: Option<u64>,
    /// Pinned temperature, or `None` when recorded as `transport_default`.
    pub temperature: Option<f64>,
    /// Normalized model configuration bound into the run fingerprint (§7.5).
    pub model_config: Value,
}

impl LoadedRunConfigV52 {
    /// `(model_id, reasoning_effort, transport_id)` entry tuple (§7.3).
    #[must_use]
    pub fn entry(&self) -> (String, String, String) {
        (
            self.model_spec.model.clone(),
            self.effort.clone(),
            self.transport_id.clone(),
        )
    }
}

/// Load a YAML run configuration (`model`, optional `model_tier`, optional `run_config`).
///
/// - Adapter-internal retries are forced off: `extra.max_retries` must be absent or `0` (§7.4).
/// - HTTP transports must state `model.max_output_tokens` explicitly: the model's vendor-maximum
///   output tokens (§7.3). There is no harness default and no combined cap.
/// - `timeout_s` defaults to the protocol's per-call timeout of 1800 s (§7.4).
pub fn load_run_config_v52(path: &Path) -> Result<LoadedRunConfigV52> {
    let raw = std::fs::read(path)
        .with_context(|| format!("failed to read model config {}", path.display()))?;
    let text = std::str::from_utf8(&raw).context("model config must be UTF-8")?;
    let mut payload: Value =
        serde_norway::from_str(text).context("model config must be valid YAML")?;
    let object = payload
        .as_object_mut()
        .ok_or_else(|| anyhow::anyhow!("model config must be a YAML mapping"))?;

    if object
        .keys()
        .any(|key| !matches!(key.as_str(), "model" | "run_config" | "model_tier"))
    {
        bail!("model config contains unsupported top-level fields");
    }

    let model_tier = match object.get("model_tier") {
        None | Some(Value::Null) => None,
        Some(Value::String(t)) if matches!(t.as_str(), "frontier" | "fast") => Some(t.clone()),
        Some(_) => bail!("model_tier must be 'frontier' or 'fast'"),
    };

    let run_config: RunConfigV52 = match object.get("run_config") {
        None | Some(Value::Null) => RunConfigV52::default(),
        Some(val @ Value::Object(_)) => serde_json::from_value(val.clone()).map_err(|e| {
            anyhow::anyhow!(
                "invalid run_config ({e}); v5.2 accepts only effort, temperature and context_window_tokens (no budget, ceiling or output-token overrides)"
            )
        })?,
        Some(_) => bail!("run_config must be a mapping"),
    };

    let model_value = object
        .get_mut("model")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| anyhow::anyhow!("model config must contain a 'model' mapping"))?;

    let explicit_model_max_output = model_value.get("max_output_tokens").and_then(Value::as_u64);
    normalize_model_extra(model_value)?;
    if !model_value.contains_key("timeout_s") {
        model_value.insert(
            "timeout_s".to_owned(),
            Value::from(PER_CALL_TIMEOUT_S_V52 as f64),
        );
    }
    let model_spec = ModelSpec::from_json_value(Value::Object(model_value.clone()))
        .map_err(|e| anyhow::anyhow!("invalid model specification: {e}"))?;

    let requested_output_tokens = if is_http_transport(model_spec.provider) {
        Some(explicit_model_max_output.ok_or_else(|| {
            anyhow::anyhow!(
                "provider '{}' requires model.max_output_tokens set to the model's vendor-maximum output tokens (SPEC-v5.2 §7.3)",
                model_spec.provider.as_str()
            )
        })?)
    } else {
        None
    };

    let effort = model_spec
        .effective_effort()
        .map(str::to_owned)
        .or_else(|| run_config.effort.clone())
        .unwrap_or_else(|| "transport_default".to_owned());
    let temperature = model_spec.temperature.or(run_config.temperature);
    let transport_id = model_spec.provider.as_str().to_owned();

    let mut normalized = serde_json::to_value(&model_spec)?;
    if let Some(map) = normalized.as_object_mut() {
        map.insert(
            "requested_output_tokens".to_owned(),
            requested_output_tokens.map_or(Value::Null, Value::from),
        );
        map.insert("reasoning_effort".to_owned(), Value::String(effort.clone()));
        map.insert(
            "sampling".to_owned(),
            temperature.map_or_else(
                || Value::String("transport_default".to_owned()),
                |t| serde_json::json!({ "temperature": t }),
            ),
        );
        map.insert(
            "context_window_tokens".to_owned(),
            run_config
                .context_window_tokens
                .map_or(Value::Null, Value::from),
        );
    }

    Ok(LoadedRunConfigV52 {
        model_spec,
        run_config,
        model_tier,
        effort,
        transport_id,
        requested_output_tokens,
        temperature,
        model_config: normalized,
    })
}

fn normalize_model_extra(model: &mut Map<String, Value>) -> Result<()> {
    let provider = model.get("provider").and_then(Value::as_str);
    let supports_retries = matches!(
        provider,
        Some(
            "anthropic"
                | "anthropic_compatible"
                | "openai_compatible"
                | "claude_cli"
                | "codex_cli"
                | "grok_cli"
                | "agy_cli"
                | "cursor_cli"
                | "muse_cli"
        )
    );
    if !supports_retries {
        return Ok(());
    }
    match model.get_mut("extra") {
        None | Some(Value::Null) => {
            let mut extra = Map::new();
            extra.insert("max_retries".to_owned(), Value::from(0));
            model.insert("extra".to_owned(), Value::Object(extra));
        }
        Some(Value::Object(extra)) => match extra.get("max_retries") {
            None => {
                extra.insert("max_retries".to_owned(), Value::from(0));
            }
            Some(v) if v.as_u64() == Some(0) => {}
            Some(_) => bail!(
                "extra.max_retries must be 0: adapters perform no internal retries (SPEC-v5.2 §7.4)"
            ),
        },
        Some(_) => {}
    }
    Ok(())
}

/// Loaded v5.2 dataset directory (`episodes.jsonl`, `oracles.jsonl`, `SEAL.json`).
#[derive(Debug, Clone)]
pub struct LoadedV52Dataset {
    pub dataset_dir: PathBuf,
    pub episodes: Vec<EpisodeV52>,
    pub oracles: Vec<SealedEpisodeOracleV52>,
    pub seal: Value,
    pub episodes_bytes: Vec<u8>,
    pub oracles_bytes: Vec<u8>,
    pub seal_bytes: Vec<u8>,
}

/// Load `episodes.jsonl`, `oracles.jsonl`, and `SEAL.json` from a v5.2 dataset directory.
pub fn load_v52_dataset(dataset_dir: &Path) -> Result<LoadedV52Dataset> {
    let episodes_path = dataset_dir.join("episodes.jsonl");
    let oracles_path = dataset_dir.join("oracles.jsonl");
    let seal_path = dataset_dir.join("SEAL.json");

    let episodes_bytes = std::fs::read(&episodes_path)
        .with_context(|| format!("failed to read {}", episodes_path.display()))?;
    let oracles_bytes = std::fs::read(&oracles_path)
        .with_context(|| format!("failed to read {}", oracles_path.display()))?;
    let seal_bytes = std::fs::read(&seal_path)
        .with_context(|| format!("failed to read {}", seal_path.display()))?;

    let episodes = parse_jsonl::<EpisodeV52>(&episodes_bytes, "episodes.jsonl")?;
    let oracles = parse_jsonl::<SealedEpisodeOracleV52>(&oracles_bytes, "oracles.jsonl")?;
    let seal: Value = serde_json::from_slice(&seal_bytes).context("SEAL.json is invalid JSON")?;

    Ok(LoadedV52Dataset {
        dataset_dir: dataset_dir.to_path_buf(),
        episodes,
        oracles,
        seal,
        episodes_bytes,
        oracles_bytes,
        seal_bytes,
    })
}

fn parse_jsonl<T: serde::de::DeserializeOwned>(bytes: &[u8], label: &str) -> Result<Vec<T>> {
    let text =
        std::str::from_utf8(bytes).with_context(|| format!("{label} must be valid UTF-8"))?;
    let mut items = Vec::new();
    for (idx, line) in text.lines().enumerate() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let item: T = serde_json::from_str(trimmed)
            .with_context(|| format!("{label} line {} is invalid JSON", idx + 1))?;
        items.push(item);
    }
    Ok(items)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_writes_are_priced_separately_from_cache_reads() {
        let rates = PricingRatesV52 {
            uncached_input_micros_per_million: 1_000_000,
            cached_input_micros_per_million: 100_000,
            cache_write_micros_per_million: Some(1_250_000),
            output_micros_per_million: 4_000_000,
            reasoning_micros_per_million: 4_000_000,
        };
        // 1M uncached + 1M cache-read + 1M cache-write + 1M output.
        let cost = rates.compute_cost_usd_micros(1_000_000, 1_000_000, 1_000_000, 1_000_000, 0);
        assert_eq!(cost, 1_000_000 + 100_000 + 1_250_000 + 4_000_000);
        // Without a cache-write rate, cache writes are priced as uncached input, never as reads.
        let no_cw = PricingRatesV52 {
            cache_write_micros_per_million: None,
            ..rates
        };
        let cost = no_cw.compute_cost_usd_micros(0, 0, 1_000_000, 0, 0);
        assert_eq!(cost, 1_000_000);
    }

    #[test]
    fn pricing_parses_cache_write_in_both_formats() {
        let micros = PricingRatesV52::from_json_value(&serde_json::json!({
            "uncached_input_micros_per_million": 1, "cached_input_micros_per_million": 2,
            "cache_write_micros_per_million": 3, "output_micros_per_million": 4,
            "reasoning_micros_per_million": 5
        }))
        .unwrap();
        assert_eq!(micros.cache_write_micros_per_million, Some(3));
        let usd = PricingRatesV52::from_json_value(&serde_json::json!({
            "input_per_1m_usd": 3.0, "cache_write_per_1m_usd": 3.75, "output_per_1m_usd": 15.0
        }))
        .unwrap();
        assert_eq!(usd.cache_write_micros_per_million, Some(3_750_000));
    }
}
