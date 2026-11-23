//! YAML run configuration, pricing rate loader, and dataset loader for IdentityBench v5.2.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use ib_accounting::{TokenCounts, TokenRates, estimate_cost_micros};
use ib_core::artifact;
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
                let micros = (f * 1_000_000.0).round();
                if !micros.is_finite() || micros >= i64::MAX as f64 {
                    bail!("pricing field '{key}' is too large to represent in micros");
                }
                Ok(micros as i64)
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

    /// The rates as `ib-accounting` [`TokenRates`] (every component rate except cache-write is
    /// always present).
    #[must_use]
    pub fn token_rates(&self) -> TokenRates {
        TokenRates {
            input: self.uncached_input_micros_per_million,
            cached_input: Some(self.cached_input_micros_per_million),
            cache_write: self.cache_write_micros_per_million,
            output: self.output_micros_per_million,
            reasoning: Some(self.reasoning_micros_per_million),
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
        estimate_cost_micros(
            &self.token_rates(),
            &TokenCounts {
                uncached_input: input_tokens,
                cache_read: cache_read_tokens,
                cache_write: cache_write_tokens,
                output: output_tokens,
                reasoning: reasoning_tokens,
            },
        )
    }
}

/// Loaded pricing artifact with raw SHA-256 digest (`pricing_sha256`).
#[derive(Debug, Clone)]
pub struct LoadedPricingV52 {
    pub artifact: PricingArtifactV52,
    pub sha256: String,
    pub raw_bytes: Vec<u8>,
}

/// Parsed form of either the legacy shared-rate pricing JSON or a frozen per-model table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PricingArtifactV52 {
    Single(PricingRatesV52),
    Table {
        as_of: String,
        models: BTreeMap<String, PricingTableEntryV52>,
    },
}

/// One model's rates and provenance in a v5.2 pricing table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PricingTableEntryV52 {
    pub rates: PricingRatesV52,
    pub source_url: String,
}

impl LoadedPricingV52 {
    /// Return the legacy shared rate or the exact model row from a pricing table.
    #[must_use]
    pub fn rates_for_model(&self, model_id: &str) -> Option<PricingRatesV52> {
        match &self.artifact {
            PricingArtifactV52::Single(rates) => Some(*rates),
            PricingArtifactV52::Table { models, .. } => {
                models.get(model_id).map(|entry| entry.rates)
            }
        }
    }

    #[must_use]
    pub fn as_of(&self) -> Option<&str> {
        match &self.artifact {
            PricingArtifactV52::Single(_) => None,
            PricingArtifactV52::Table { as_of, .. } => Some(as_of),
        }
    }

    #[must_use]
    pub fn source_url_for_model(&self, model_id: &str) -> Option<&str> {
        match &self.artifact {
            PricingArtifactV52::Single(_) => None,
            PricingArtifactV52::Table { models, .. } => {
                models.get(model_id).map(|entry| entry.source_url.as_str())
            }
        }
    }
}

const PRICING_TABLE_SCHEMA_V52: &str = "identitybench-v52-pricing-table-v1";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PricingTableV52 {
    #[serde(rename = "schema")]
    _schema: String,
    as_of: String,
    models: BTreeMap<String, Value>,
}

fn parse_pricing_table_model_v52(value: &Value, model_id: &str) -> Result<PricingTableEntryV52> {
    let object = value
        .as_object()
        .ok_or_else(|| anyhow::anyhow!("pricing table model '{model_id}' must be an object"))?;
    let source_url = object
        .get("source_url")
        .and_then(Value::as_str)
        .filter(|url| !url.trim().is_empty())
        .ok_or_else(|| {
            anyhow::anyhow!("pricing table model '{model_id}' source_url must be non-empty")
        })?
        .to_owned();
    if !object.contains_key("uncached_input_micros_per_million") {
        bail!("pricing table model '{model_id}' requires integer micros per million");
    }
    let rates = PricingRatesV52::from_json_value(value)
        .with_context(|| format!("invalid pricing rates for model '{model_id}'"))?;
    if rates.cache_write_micros_per_million.is_none() {
        bail!(
            "pricing table model '{model_id}' requires an integer cache_write_micros_per_million"
        );
    }
    Ok(PricingTableEntryV52 { rates, source_url })
}

fn validate_pricing_date(value: &str) -> Result<()> {
    let bytes = value.as_bytes();
    if bytes.len() != 10
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || !bytes
            .iter()
            .enumerate()
            .all(|(i, byte)| matches!(i, 4 | 7) || byte.is_ascii_digit())
    {
        bail!("pricing table as_of must be a valid YYYY-MM-DD date");
    }
    let year: u32 = value[0..4].parse()?;
    let month: u32 = value[5..7].parse()?;
    let day: u32 = value[8..10].parse()?;
    let leap_year =
        year.is_multiple_of(4) && (!year.is_multiple_of(100) || year.is_multiple_of(400));
    let days_in_month = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if leap_year => 29,
        2 => 28,
        _ => 0,
    };
    if !(1..=days_in_month).contains(&day) {
        bail!("pricing table as_of must be a valid YYYY-MM-DD date");
    }
    Ok(())
}

pub fn load_pricing_v52(path: &Path) -> Result<LoadedPricingV52> {
    let raw_bytes = artifact::read_limited(path, 4 * 1024 * 1024)
        .with_context(|| format!("failed to read pricing {}", path.display()))?;
    let value: Value = serde_json::from_slice(&raw_bytes).context("pricing JSON is invalid")?;
    let artifact = match value
        .as_object()
        .and_then(|obj| obj.get("schema"))
        .and_then(Value::as_str)
    {
        Some(PRICING_TABLE_SCHEMA_V52) => {
            let table: PricingTableV52 = serde_json::from_value(value)
                .context("pricing table does not match identitybench-v52-pricing-table-v1")?;
            validate_pricing_date(&table.as_of)?;
            if table.models.is_empty() {
                bail!("pricing table models must contain at least one model row");
            }
            let models = table
                .models
                .into_iter()
                .map(|(model_id, row)| {
                    if model_id.trim().is_empty() {
                        bail!("pricing table model_id must be non-empty");
                    }
                    Ok((
                        model_id.clone(),
                        parse_pricing_table_model_v52(&row, &model_id)?,
                    ))
                })
                .collect::<Result<BTreeMap<_, _>>>()?;
            PricingArtifactV52::Table {
                as_of: table.as_of,
                models,
            }
        }
        _ => PricingArtifactV52::Single(PricingRatesV52::from_json_value(&value)?),
    };
    let sha256 = sha256_hex(&raw_bytes);
    Ok(LoadedPricingV52 {
        artifact,
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
    let raw = artifact::read_limited(path, 4 * 1024 * 1024)
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
    let root = artifact::directory(dataset_dir, false)?;
    let episodes_path = dataset_dir.join("episodes.jsonl");
    let oracles_path = dataset_dir.join("oracles.jsonl");
    let seal_path = dataset_dir.join("SEAL.json");

    let episodes_bytes = artifact::read_at(&root, Path::new("episodes.jsonl"))
        .with_context(|| format!("failed to read {}", episodes_path.display()))?;
    let oracles_bytes = artifact::read_at(&root, Path::new("oracles.jsonl"))
        .with_context(|| format!("failed to read {}", oracles_path.display()))?;
    let seal_bytes = artifact::read_limited_at(&root, Path::new("SEAL.json"), 16 * 1024 * 1024)
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
    ib_v52_schemas::jsonl::parse_jsonl(bytes).map_err(|err| match err {
        ib_v52_schemas::jsonl::JsonlError::Utf8(source) => {
            anyhow::Error::new(source).context(format!("{label} must be valid UTF-8"))
        }
        ib_v52_schemas::jsonl::JsonlError::Line { line, source } => {
            anyhow::Error::new(source).context(format!("{label} line {line} is invalid JSON"))
        }
    })
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

    #[test]
    fn pricing_rejects_usd_rates_that_overflow_micros() {
        let err = PricingRatesV52::from_json_value(&serde_json::json!({
            "input_per_1m_usd": 1.0e300,
            "output_per_1m_usd": 0.0
        }))
        .unwrap_err();
        assert!(err.to_string().contains("too large to represent in micros"));
    }
}

#[cfg(all(test, unix))]
mod dataset_path_tests {
    use super::load_v52_dataset;

    #[test]
    fn dataset_loader_requires_regular_sealed_inputs() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("episodes.jsonl")).unwrap();
        let err = load_v52_dataset(dir.path()).unwrap_err();
        assert!(err.to_string().contains("episodes.jsonl"));
    }
}
