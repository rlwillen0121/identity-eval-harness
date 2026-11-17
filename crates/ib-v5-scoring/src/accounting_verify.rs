//! Usage-accounting manifest and attestation artifact verification (`G-ECON-01` provenance).

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

use ib_core::hashing::sha256_hex;
use ib_core::jcs_dumps;
use ib_v5_accounting::{
    USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5, UsageAccountingAttestationV5,
    UsageAccountingManifestV5,
};
use ib_v5_schemas::{SHA256_HEX_RE, StrictV5Model};
use serde::de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Number, Value};

use crate::error::ScoringError;

/// One verified per-role usage-accounting entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedUsageAccountingEntryV5 {
    pub role: String,
    pub provider_kind: String,
    pub model_id: String,
    pub provider_fingerprint_sha256: String,
    pub aggregate_usage_scope: String,
    pub evidence_sha256: String,
    pub adapter_retry_limit: i64,
    pub attestation: String,
}

/// Manifest and evidence bytes verified before provider accounting is scored.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedUsageAccountingV5 {
    pub manifest_sha256: String,
    pub entries_by_role: BTreeMap<String, VerifiedUsageAccountingEntryV5>,
}

struct UniqueJsonSeed<'a> {
    label: &'a str,
}

impl<'de> DeserializeSeed<'de> for UniqueJsonSeed<'_> {
    type Value = Value;

    fn deserialize<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        deserializer.deserialize_any(UniqueJsonVisitor { label: self.label })
    }
}

struct UniqueJsonVisitor<'a> {
    label: &'a str,
}

impl<'de> Visitor<'de> for UniqueJsonVisitor<'_> {
    type Value = Value;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("any valid JSON value without duplicate object keys")
    }

    fn visit_bool<E>(self, v: bool) -> Result<Self::Value, E> {
        Ok(Value::Bool(v))
    }

    fn visit_i64<E>(self, v: i64) -> Result<Self::Value, E> {
        Ok(Value::Number(Number::from(v)))
    }

    fn visit_u64<E>(self, v: u64) -> Result<Self::Value, E> {
        Ok(Value::Number(Number::from(v)))
    }

    fn visit_f64<E>(self, v: f64) -> Result<Self::Value, E>
    where
        E: de::Error,
    {
        Number::from_f64(v)
            .map(Value::Number)
            .ok_or_else(|| E::custom("non-finite float in JSON"))
    }

    fn visit_str<E>(self, v: &str) -> Result<Self::Value, E> {
        Ok(Value::String(v.to_owned()))
    }

    fn visit_string<E>(self, v: String) -> Result<Self::Value, E> {
        Ok(Value::String(v))
    }

    fn visit_none<E>(self) -> Result<Self::Value, E> {
        Ok(Value::Null)
    }

    fn visit_unit<E>(self) -> Result<Self::Value, E> {
        Ok(Value::Null)
    }

    fn visit_some<D>(self, deserializer: D) -> Result<Self::Value, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        UniqueJsonSeed { label: self.label }.deserialize(deserializer)
    }

    fn visit_seq<A>(self, mut seq: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        let mut items = Vec::new();
        while let Some(elem) = seq.next_element_seed(UniqueJsonSeed { label: self.label })? {
            items.push(elem);
        }
        Ok(Value::Array(items))
    }

    fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
    where
        A: MapAccess<'de>,
    {
        let Some(first_key) = map.next_key::<String>()? else {
            return Ok(Value::Object(Map::new()));
        };
        // With `serde_json`'s `arbitrary_precision` feature enabled, numbers arrive via
        // `visit_map` with the internal marker key `"$serde_json::private::Number"`.
        if first_key == "$serde_json::private::Number" {
            let text: String = map.next_value()?;
            let number: Number = text
                .parse()
                .map_err(|err| de::Error::custom(format!("invalid JSON number: {err}")))?;
            return Ok(Value::Number(number));
        }

        let mut out = Map::new();
        let first_val = map.next_value_seed(UniqueJsonSeed { label: self.label })?;
        out.insert(first_key, first_val);

        while let Some(key) = map.next_key::<String>()? {
            if out.contains_key(&key) {
                return Err(de::Error::custom(format!(
                    "{} contains duplicate JSON key {:?}",
                    self.label, key
                )));
            }
            let val = map.next_value_seed(UniqueJsonSeed { label: self.label })?;
            out.insert(key, val);
        }
        Ok(Value::Object(out))
    }
}

pub(crate) fn parse_json_without_duplicate_keys(
    raw: &[u8],
    label: &str,
) -> Result<Value, ScoringError> {
    let text = std::str::from_utf8(raw)
        .map_err(|_| ScoringError::value(format!("{label} is not valid UTF-8 JSON")))?;
    let mut de = serde_json::Deserializer::from_str(text);
    let value = UniqueJsonSeed { label }
        .deserialize(&mut de)
        .map_err(|err| {
            let msg = err.to_string();
            if msg.contains("contains duplicate JSON key") {
                ScoringError::value(msg)
            } else {
                ScoringError::value(format!("{label} is not valid UTF-8 JSON: {msg}"))
            }
        })?;
    de.end()
        .map_err(|err| ScoringError::value(format!("{label} is not valid UTF-8 JSON: {err}")))?;
    Ok(value)
}

fn is_unsafe_posix_evidence_path(path: &str) -> bool {
    path.starts_with('/')
        || path.contains('\\')
        || path.split('/').any(|part| part == "..")
}

/// Verify exact usage-accounting manifest and attestation bytes by role.
pub fn verify_usage_accounting_artifacts_v5(
    manifest_raw: &[u8],
    evidence_by_role_raw: &BTreeMap<String, Vec<u8>>,
    expected_manifest_sha256: &str,
) -> Result<VerifiedUsageAccountingV5, ScoringError> {
    if !SHA256_HEX_RE.is_match(expected_manifest_sha256) {
        return Err(ScoringError::value(
            "usage-accounting manifest digest must be lowercase SHA-256",
        ));
    }
    let actual_manifest_sha = sha256_hex(manifest_raw);
    if actual_manifest_sha != expected_manifest_sha256 {
        return Err(ScoringError::value(
            "usage-accounting manifest bytes disagree with their SHA-256",
        ));
    }

    let manifest_json = parse_json_without_duplicate_keys(manifest_raw, "usage-accounting manifest")?;
    if !manifest_json.is_object() || jcs_dumps(&manifest_json)?.as_slice() != manifest_raw {
        return Err(ScoringError::value(
            "usage-accounting manifest must use canonical JCS bytes",
        ));
    }
    let manifest_str = std::str::from_utf8(manifest_raw)
        .map_err(|_| ScoringError::value("usage-accounting manifest is not valid UTF-8 JSON"))?;
    let manifest = UsageAccountingManifestV5::from_strict_json_str(manifest_str).map_err(|_| {
        ScoringError::value("usage-accounting manifest failed strict schema validation")
    })?;

    let expected_roles: BTreeSet<String> = manifest
        .entries
        .iter()
        .map(|e| e.role.as_str().to_owned())
        .collect();
    let provided_roles: BTreeSet<String> = evidence_by_role_raw.keys().cloned().collect();
    if provided_roles != expected_roles {
        return Err(ScoringError::value(
            "usage-accounting evidence role set disagrees with its manifest",
        ));
    }

    let mut verified_entries = BTreeMap::new();
    for entry in &manifest.entries {
        let role_str = entry.role.as_str();
        if is_unsafe_posix_evidence_path(&entry.evidence_path) {
            return Err(ScoringError::value(format!(
                "usage-accounting source path is unsafe for {role_str:?}"
            )));
        }
        let evidence_raw = evidence_by_role_raw.get(role_str).ok_or_else(|| {
            ScoringError::value("usage-accounting evidence role set disagrees with its manifest")
        })?;
        let actual_evidence_sha = sha256_hex(evidence_raw);
        if actual_evidence_sha != entry.evidence_sha256 {
            return Err(ScoringError::value(format!(
                "usage-accounting evidence digest mismatch for {role_str:?}"
            )));
        }
        let evidence_label = format!("usage-accounting evidence for {role_str:?}");
        parse_json_without_duplicate_keys(evidence_raw, &evidence_label)?;
        let evidence_str = std::str::from_utf8(evidence_raw).map_err(|_| {
            ScoringError::value(format!(
                "usage-accounting evidence for {role_str:?} is not valid UTF-8 JSON"
            ))
        })?;
        let attestation = UsageAccountingAttestationV5::from_strict_json_str(evidence_str)
            .map_err(|_| {
                ScoringError::value(format!(
                    "usage-accounting evidence for {role_str:?} failed strict schema validation"
                ))
            })?;

        if attestation.role != entry.role
            || attestation.provider_kind != entry.provider_kind
            || attestation.model_id != entry.model_id
            || attestation.provider_fingerprint_sha256 != entry.provider_fingerprint_sha256
            || attestation.aggregate_usage_scope != entry.aggregate_usage_scope
        {
            return Err(ScoringError::value(format!(
                "usage-accounting evidence identity disagrees with manifest for {role_str:?}"
            )));
        }
        if entry.aggregate_usage_scope != USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5 {
            return Err(ScoringError::value(format!(
                "usage-accounting scope is incomplete for {role_str:?}"
            )));
        }
        if entry.adapter_retry_limit != 0 {
            return Err(ScoringError::value(format!(
                "local retries are enabled for {role_str:?}"
            )));
        }
        verified_entries.insert(
            role_str.to_owned(),
            VerifiedUsageAccountingEntryV5 {
                role: role_str.to_owned(),
                provider_kind: entry.provider_kind.clone(),
                model_id: entry.model_id.clone(),
                provider_fingerprint_sha256: entry.provider_fingerprint_sha256.clone(),
                aggregate_usage_scope: entry.aggregate_usage_scope.clone(),
                evidence_sha256: entry.evidence_sha256.clone(),
                adapter_retry_limit: entry.adapter_retry_limit,
                attestation: attestation.attestation,
            },
        );
    }

    Ok(VerifiedUsageAccountingV5 {
        manifest_sha256: actual_manifest_sha,
        entries_by_role: verified_entries,
    })
}
