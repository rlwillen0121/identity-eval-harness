//! YAML run configuration, pricing rate loader, and dataset loader for IdentityBench v5.1.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use ib_core::hashing::sha256_hex;
use ib_providers::factory::ModelSpec;
use ib_v5_accounting::{FrozenPricingRateV5, HasUsageTokenCounts, estimate_frozen_cost_micros_v5};
use ib_v51_schemas::{EpisodeV51, SealedEpisodeOracleV51};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub const DEFAULT_V51_MAX_OUTPUT_TOKENS: u64 = 32_768;

/// Pricing rates in USD micros per 1M tokens.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct PricingRatesV51 {
    pub uncached_input_micros_per_million: i64,
    pub cached_input_micros_per_million: i64,
    pub output_micros_per_million: i64,
    pub reasoning_micros_per_million: i64,
}

impl PricingRatesV51 {
    /// Parse either `ib-v5-runner` micros-per-million JSON or calibration USD-per-1M JSON.
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
            return Ok(Self {
                uncached_input_micros_per_million: get_micros("uncached_input_micros_per_million")?,
                cached_input_micros_per_million: get_micros("cached_input_micros_per_million")?,
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
            let output = get_usd_micros("output_per_1m_usd")?;
            let reasoning = match obj.get("reasoning_per_1m_usd") {
                None | Some(Value::Null) => output,
                Some(_) => get_usd_micros("reasoning_per_1m_usd")?,
            };
            return Ok(Self {
                uncached_input_micros_per_million: uncached,
                cached_input_micros_per_million: cached,
                output_micros_per_million: output,
                reasoning_micros_per_million: reasoning,
            });
        }

        bail!(
            "unrecognized pricing JSON schema; expected uncached_input_micros_per_million or input_per_1m_usd"
        )
    }

    /// Convert to `ib-v5-accounting`'s `FrozenPricingRateV5` representation.
    #[must_use]
    pub fn to_frozen_rate(&self, pricing_manifest_sha256: &str) -> FrozenPricingRateV5 {
        FrozenPricingRateV5 {
            pricing_manifest_sha256: pricing_manifest_sha256.to_owned(),
            currency_code: "USD".to_owned(),
            input_cost_per_million_micros: self.uncached_input_micros_per_million,
            cached_input_cost_per_million_micros: Some(self.cached_input_micros_per_million),
            cache_write_cost_per_million_micros: None,
            output_cost_per_million_micros: self.output_micros_per_million,
            reasoning_cost_per_million_micros: Some(self.reasoning_micros_per_million),
        }
    }

    /// Compute rounded integer USD micros from accumulated token counts.
    #[must_use]
    pub fn compute_cost_usd_micros(
        &self,
        input_tokens: u64,
        cached_input_tokens: u64,
        output_tokens: u64,
        reasoning_tokens: u64,
    ) -> u64 {
        struct TokenSnapshot {
            total_in: i64,
            cached_in: i64,
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
        }
        let snapshot = TokenSnapshot {
            total_in: i64::try_from(input_tokens.saturating_add(cached_input_tokens))
                .unwrap_or(i64::MAX),
            cached_in: i64::try_from(cached_input_tokens).unwrap_or(i64::MAX),
            total_out: i64::try_from(output_tokens).unwrap_or(i64::MAX),
            reasoning: i64::try_from(reasoning_tokens).unwrap_or(i64::MAX),
        };
        let frozen = self.to_frozen_rate(&"0".repeat(64));
        if let Some(micros) = estimate_frozen_cost_micros_v5(&snapshot, &frozen, None, None)
            && micros >= 0
        {
            return micros as u64;
        }
        let visible_output = output_tokens.saturating_sub(reasoning_tokens);
        let numerator = i128::from(input_tokens)
            * i128::from(self.uncached_input_micros_per_million)
            + i128::from(cached_input_tokens) * i128::from(self.cached_input_micros_per_million)
            + i128::from(visible_output) * i128::from(self.output_micros_per_million)
            + i128::from(reasoning_tokens) * i128::from(self.reasoning_micros_per_million);
        if numerator <= 0 {
            0
        } else {
            u64::try_from((numerator + 500_000) / 1_000_000).unwrap_or(u64::MAX)
        }
    }
}

/// Loaded pricing artifact with raw SHA-256 digest.
#[derive(Debug, Clone)]
pub struct LoadedPricingV51 {
    pub rates: PricingRatesV51,
    pub sha256: String,
    pub raw_bytes: Vec<u8>,
}

pub fn load_pricing_v51(path: &Path) -> Result<LoadedPricingV51> {
    let raw_bytes = std::fs::read(path)
        .with_context(|| format!("failed to read pricing {}", path.display()))?;
    let value: Value = serde_json::from_slice(&raw_bytes).context("pricing JSON is invalid")?;
    let rates = PricingRatesV51::from_json_value(&value)?;
    let sha256 = sha256_hex(&raw_bytes);
    Ok(LoadedPricingV51 {
        rates,
        sha256,
        raw_bytes,
    })
}

/// Execution parameters parsed from the optional `run_config` YAML section.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct RunConfigV51 {
    pub effort: Option<String>,
    pub max_output_tokens: u64,
    pub max_assistant_generations: Option<usize>,
    pub max_calls_per_turn: Option<usize>,
    pub temperature: Option<f64>,
}

impl Default for RunConfigV51 {
    fn default() -> Self {
        Self {
            effort: None,
            max_output_tokens: DEFAULT_V51_MAX_OUTPUT_TOKENS,
            max_assistant_generations: None,
            max_calls_per_turn: None,
            temperature: None,
        }
    }
}

/// Complete YAML run configuration (`model`, optional `model_tier`, optional `run_config`).
#[derive(Debug, Clone)]
pub struct LoadedRunConfigV51 {
    pub model_spec: ModelSpec,
    pub run_config: RunConfigV51,
    pub model_tier: Option<String>,
    pub effort: String,
    pub max_output_tokens: u64,
}

/// Load a calibration/v5-compatible YAML run configuration.
pub fn load_run_config_v51(path: &Path) -> Result<LoadedRunConfigV51> {
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

    let model_value = object
        .get_mut("model")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| anyhow::anyhow!("model config must contain a 'model' mapping"))?;

    let explicit_model_max_output = model_value.get("max_output_tokens").and_then(Value::as_u64);
    normalize_model_extra(model_value);
    let model_spec = ModelSpec::from_json_value(Value::Object(model_value.clone()))
        .map_err(|e| anyhow::anyhow!("invalid model specification: {e}"))?;

    let run_config = match object.get("run_config") {
        None | Some(Value::Null) => RunConfigV51::default(),
        Some(val @ Value::Object(_)) => parse_run_config_section(val)?,
        Some(_) => bail!("run_config must be a mapping"),
    };

    let effort = model_spec
        .effective_effort()
        .map(str::to_owned)
        .or_else(|| run_config.effort.clone())
        .unwrap_or_else(|| "medium".to_owned());

    let max_output_tokens = explicit_model_max_output.unwrap_or(run_config.max_output_tokens);

    Ok(LoadedRunConfigV51 {
        model_spec,
        run_config,
        model_tier,
        effort,
        max_output_tokens,
    })
}

fn parse_run_config_section(value: &Value) -> Result<RunConfigV51> {
    let map = value
        .as_object()
        .ok_or_else(|| anyhow::anyhow!("run_config must be a mapping"))?;
    let mut cfg = RunConfigV51::default();
    if let Some(eff) = map.get("effort").and_then(Value::as_str) {
        cfg.effort = Some(eff.to_owned());
    }
    if let Some(toks) = map.get("max_output_tokens").and_then(Value::as_u64) {
        cfg.max_output_tokens = toks;
    }
    if let Some(gens) = map
        .get("max_assistant_generations")
        .or_else(|| map.get("max_assistant_generations_per_episode"))
        .and_then(Value::as_u64)
    {
        cfg.max_assistant_generations = Some(usize::try_from(gens)?);
    }
    if let Some(calls) = map.get("max_calls_per_turn").and_then(Value::as_u64) {
        cfg.max_calls_per_turn = Some(usize::try_from(calls)?);
    }
    if let Some(temp) = map.get("temperature").and_then(Value::as_f64) {
        cfg.temperature = Some(temp);
    }
    Ok(cfg)
}

fn normalize_model_extra(model: &mut Map<String, Value>) {
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
        return;
    }
    match model.get_mut("extra") {
        None | Some(Value::Null) => {
            let mut extra = Map::new();
            extra.insert("max_retries".to_owned(), Value::from(0));
            model.insert("extra".to_owned(), Value::Object(extra));
        }
        Some(Value::Object(extra)) => {
            extra
                .entry("max_retries".to_owned())
                .or_insert(Value::from(0));
        }
        Some(_) => {}
    }
}

/// Loaded v5.1 dataset directory (`episodes.jsonl`, `oracles.jsonl`, `SEAL.json`).
#[derive(Debug, Clone)]
pub struct LoadedV51Dataset {
    pub dataset_dir: PathBuf,
    pub episodes: Vec<EpisodeV51>,
    pub oracles: Vec<SealedEpisodeOracleV51>,
    pub seal: Value,
    pub episodes_bytes: Vec<u8>,
    pub oracles_bytes: Vec<u8>,
    pub seal_bytes: Vec<u8>,
}

/// Load `episodes.jsonl`, `oracles.jsonl`, and `SEAL.json` from a v5.1 dataset directory.
pub fn load_v51_dataset(dataset_dir: &Path) -> Result<LoadedV51Dataset> {
    let episodes_path = dataset_dir.join("episodes.jsonl");
    let oracles_path = dataset_dir.join("oracles.jsonl");
    let seal_path = dataset_dir.join("SEAL.json");

    let episodes_bytes = std::fs::read(&episodes_path)
        .with_context(|| format!("failed to read {}", episodes_path.display()))?;
    let oracles_bytes = std::fs::read(&oracles_path)
        .with_context(|| format!("failed to read {}", oracles_path.display()))?;
    let seal_bytes = std::fs::read(&seal_path)
        .with_context(|| format!("failed to read {}", seal_path.display()))?;

    let episodes = parse_jsonl::<EpisodeV51>(&episodes_bytes, "episodes.jsonl")?;
    let oracles = parse_jsonl::<SealedEpisodeOracleV51>(&oracles_bytes, "oracles.jsonl")?;
    let seal: Value = serde_json::from_slice(&seal_bytes).context("SEAL.json is invalid JSON")?;

    Ok(LoadedV51Dataset {
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
