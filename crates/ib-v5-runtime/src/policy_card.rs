//! Deterministic compact, agent-visible v5 policy card and size gates.
//!
//! Mirrors `identitybench.v5.policy_card`.

use std::collections::BTreeSet;
use std::sync::LazyLock;

use ib_core::pyjson::{DumpOptions, JsonError, dumps_ser};
use ib_v5_schemas::{
    PolicyCardV5, PolicyOverlayV5, SchemaValidationError, ScopeCatalogV5, StrictV5Model,
};
use indexmap::IndexMap;
use serde::Serialize;
use serde_json::Value;
use thiserror::Error;
use tiktoken_rs::CoreBPE;

/// Deprecated pre-draft.5 token limits retained temporarily for compatibility.
pub const POLICY_CARD_TOKEN_LIMIT_V5: usize = 3_200;
pub const LOGICAL_INPUT_TOKEN_LIMIT_V5: usize = 9_600;

pub const RULE_IDS_V5: [&str; 26] = [
    "CONN-V5-01",
    "ITSM-V5-01",
    "HR-AUTH-V5-01",
    "BIND-V5-01a",
    "BIND-V5-01b",
    "CONV-V5-02",
    "MOVER-V5-03",
    "CC-V5-01",
    "SOD-V5-01",
    "NHI-V5-01",
    "AGENT-REG-V5-01",
    "CIEM-V5-01",
    "OPT-V5-01",
    "OPT-V5-02",
    "JML-WF-V5-01",
    "CAE-V5-01",
    "SHOCK-V5-01",
    "UAR-V5-01",
    "JIT-V5-01.P1",
    "JIT-V5-01.P2",
    "JIT-V5-01.P3",
    "JIT-V5-01.P4",
    "AGENT-DEL-V5-01.D1",
    "AGENT-DEL-V5-01.D2",
    "AGENT-DEL-V5-01.D3",
    "AGENT-DEL-V5-01.D4",
];

pub const BOUNDARY_ANCHORS_V5: [&str; 8] = [
    "SCOPES",
    "PRECOND_CITE",
    "TOKENS",
    "STAGE5",
    "SAFETY",
    "SNAPSHOTS",
    "SCORE_BUDGETS",
    "PHASE1_LEDGER",
];

pub const BOUNDARY_ALIASES_V5: [(&str, &str); 5] = [
    ("ANTI_ORACLE", "TOOLS"),
    ("QUERY", "TOOLS"),
    ("STAGING", "TOOLS"),
    ("AGENT", "AGENT-REG-V5-01"),
    ("JML", "JML-WF-V5-01"),
];

pub static RULE_TO_POLICY_CARD_ANCHORS_V5: LazyLock<IndexMap<String, Vec<String>>> =
    LazyLock::new(|| {
        let mut map = IndexMap::new();
        for rule_id in RULE_IDS_V5 {
            map.insert(rule_id.to_owned(), vec![rule_id.to_owned()]);
        }
        for anchor in BOUNDARY_ANCHORS_V5 {
            map.insert(format!("BOUNDARY:{anchor}"), vec![anchor.to_owned()]);
        }
        for (anchor, section_id) in BOUNDARY_ALIASES_V5 {
            map.insert(format!("BOUNDARY:{anchor}"), vec![section_id.to_owned()]);
        }
        map
    });

const CANONICAL_RULES_MARKDOWN: &str = include_str!("rules_markdown_v5.md");

static O200K_ENCODER: LazyLock<CoreBPE> =
    LazyLock::new(|| tiktoken_rs::o200k_base().expect("o200k_base BPE must load"));

#[derive(Debug, Error)]
pub enum PolicyCardError {
    #[error("Policy card coverage failure: {0}")]
    Coverage(String),
    #[error("Schema validation failed: {0}")]
    Schema(#[from] SchemaValidationError),
    #[error("JSON serialization failed: {0}")]
    Json(#[from] JsonError),
}

/// Return the stable policy text; dynamic policy values remain in the overlay.
#[must_use]
pub fn render_rules_markdown() -> String {
    CANONICAL_RULES_MARKDOWN.trim().to_owned() + "\n"
}

/// Split level-three anchored clauses into normalized ids and body text.
#[must_use]
pub fn policy_card_sections(markdown: &str) -> IndexMap<String, String> {
    let mut sections = IndexMap::new();
    let mut current: Option<String> = None;
    let mut body: Vec<&str> = Vec::new();
    for line in markdown.lines() {
        if let Some(heading) = line.strip_prefix("### ") {
            if let Some(prev) = current.take() {
                sections.insert(prev, body.join("\n").trim().to_owned());
            }
            current = Some(heading.trim().to_owned());
            body.clear();
        } else if current.is_some() {
            body.push(line);
        }
    }
    if let Some(prev) = current {
        sections.insert(prev, body.join("\n").trim().to_owned());
    }
    sections
}

/// Check authored anchor presence and identity; this does not prove spec completeness.
#[must_use]
pub fn validate_policy_card_coverage(markdown: &str) -> Vec<String> {
    let mut errors = Vec::new();
    let sections = policy_card_sections(markdown);
    let canonical_sections = policy_card_sections(CANONICAL_RULES_MARKDOWN);

    let mut required_ids = BTreeSet::new();
    for section_ids in RULE_TO_POLICY_CARD_ANCHORS_V5.values() {
        for section_id in section_ids {
            required_ids.insert(section_id.as_str());
        }
    }

    let heading_ids: Vec<&str> = markdown
        .lines()
        .filter_map(|line| line.strip_prefix("### ").map(str::trim))
        .collect();

    for section_id in required_ids {
        let occurrences = heading_ids.iter().filter(|&&h| h == section_id).count();
        if occurrences > 1 {
            errors.push(format!(
                "duplicate section {section_id} ({occurrences} copies)"
            ));
        }
    }

    for (anchor_id, section_ids) in RULE_TO_POLICY_CARD_ANCHORS_V5.iter() {
        for section_id in section_ids {
            let body = sections.get(section_id);
            let canonical_body = canonical_sections.get(section_id);
            match body {
                None => errors.push(format!("missing section {anchor_id} at {section_id}")),
                Some(b) if Some(b) != canonical_body => {
                    errors.push(format!(
                        "section {anchor_id} differs from its authored baseline"
                    ));
                }
                Some(_) => {}
            }
        }
    }

    errors
}

/// Construct an agent-visible card without generator-only regime metadata.
pub fn render_policy_card(
    scope_catalog: &ScopeCatalogV5,
    overlay: &PolicyOverlayV5,
) -> Result<PolicyCardV5, PolicyCardError> {
    let card = PolicyCardV5 {
        spec_version: "5.0".to_owned(),
        scope_catalog: scope_catalog.clone(),
        overlay: overlay.clone(),
        rendered_rules_markdown: render_rules_markdown(),
    };
    card.validate()?;
    let errors = validate_policy_card_coverage(&card.rendered_rules_markdown);
    if !errors.is_empty() {
        return Err(PolicyCardError::Coverage(errors.join("; ")));
    }
    Ok(card)
}

fn serialize_model<T: Serialize + ?Sized>(value: &T) -> Result<String, PolicyCardError> {
    Ok(dumps_ser(value, &DumpOptions::compact_sorted())?)
}

/// Count the full deterministic serialized card with the normative `o200k_base` encoding.
pub fn count_policy_card_tokens(card: &PolicyCardV5) -> Result<usize, PolicyCardError> {
    let serialized = serialize_model(card)?;
    Ok(O200K_ENCODER.encode_ordinary(&serialized).len())
}

/// Return rule/anchor coverage and non-truncation violations (no 3,200 token ceiling).
#[must_use]
pub fn validate_policy_card(card: &PolicyCardV5) -> Vec<String> {
    let mut errors = validate_policy_card_coverage(&card.rendered_rules_markdown);
    if card.spec_version != "5.0" {
        errors.push(format!(
            "unexpected policy card spec_version {:?}",
            card.spec_version
        ));
    }
    if card.scope_catalog.leaf_scopes.is_empty() {
        errors.push("policy card scope_catalog.leaf_scopes must be non-empty".to_owned());
    }
    if card.rendered_rules_markdown.trim() != render_rules_markdown().trim() {
        let msg =
            "policy card rendered_rules_markdown does not match complete canonical text".to_owned();
        if !errors.contains(&msg) {
            errors.push(msg);
        }
    }
    errors
}

/// Typed logical prompt part accepted by [`count_logical_episode_tokens`] and
/// [`validate_logical_episode_tokens`].
#[derive(Debug, Clone)]
pub enum LogicalInput<'a> {
    None,
    Str(&'a str),
    PolicyCard(&'a PolicyCardV5),
    Json(&'a Value),
    Serialized(String),
}

impl<'a> LogicalInput<'a> {
    /// Construct a [`LogicalInput`] from any `Serialize` model (matching Pydantic `model_dump(mode="json")`).
    pub fn from_model<T: Serialize + ?Sized>(value: &T) -> Result<Self, PolicyCardError> {
        Ok(Self::Serialized(serialize_model(value)?))
    }

    fn serialize_part(&self) -> Result<String, PolicyCardError> {
        match self {
            Self::None => Ok("null".to_owned()),
            Self::Str(s) => Ok((*s).to_owned()),
            Self::PolicyCard(card) => serialize_model(*card),
            Self::Json(val) => {
                if let Some(s) = val.as_str() {
                    Ok(s.to_owned())
                } else {
                    Ok(ib_core::pyjson::dumps(val, &DumpOptions::compact_sorted())?)
                }
            }
            Self::Serialized(s) => Ok(s.clone()),
        }
    }
}

impl<'a> From<&'a str> for LogicalInput<'a> {
    fn from(s: &'a str) -> Self {
        Self::Str(s)
    }
}

impl<'a> From<&'a String> for LogicalInput<'a> {
    fn from(s: &'a String) -> Self {
        Self::Str(s.as_str())
    }
}

impl<'a> From<&'a PolicyCardV5> for LogicalInput<'a> {
    fn from(card: &'a PolicyCardV5) -> Self {
        Self::PolicyCard(card)
    }
}

impl<'a> From<&'a Value> for LogicalInput<'a> {
    fn from(val: &'a Value) -> Self {
        Self::Json(val)
    }
}

/// Count deterministic logical prompt parts, including each serialized payload.
pub fn count_logical_episode_tokens(parts: &[LogicalInput<'_>]) -> Result<usize, PolicyCardError> {
    let mut serialized_parts = Vec::with_capacity(parts.len());
    for part in parts {
        serialized_parts.push(part.serialize_part()?);
    }
    let joined = serialized_parts.join("\n");
    Ok(O200K_ENCODER.encode_ordinary(&joined).len())
}

/// Verify logical-input completeness/non-truncation (no 9,600 token ceiling).
pub fn validate_logical_episode_tokens(
    parts: &[LogicalInput<'_>],
    max_context_tokens: Option<usize>,
) -> Result<Vec<String>, PolicyCardError> {
    if parts.is_empty() {
        return Ok(vec!["logical episode inputs must not be empty".to_owned()]);
    }
    let mut errors = Vec::new();
    for (index, part) in parts.iter().enumerate() {
        match part {
            LogicalInput::None => {
                errors.push(format!("logical episode input part {index} is None"));
            }
            LogicalInput::PolicyCard(card) => {
                errors.extend(validate_policy_card(card));
            }
            LogicalInput::Str(s) if s.trim().is_empty() => {
                errors.push(format!(
                    "logical episode string part {index} is empty or truncated"
                ));
            }
            LogicalInput::Serialized(s) if s.trim().is_empty() => {
                errors.push(format!(
                    "logical episode string part {index} is empty or truncated"
                ));
            }
            _ => {}
        }
    }
    if let Some(max_tokens) = max_context_tokens {
        let token_count = count_logical_episode_tokens(parts)?;
        if token_count > max_tokens {
            errors.push(format!(
                "logical episode inputs are {token_count} o200k_base tokens; provider context limit is {max_tokens}"
            ));
        }
    }
    Ok(errors)
}
