//! Versioned normative input bindings for v5 conformance proofs.
//!
//! Mirrors `identitybench.v5.normative_contract`.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub const V5_BASE_SPEC_PATH: &str = "SPEC-v5.md";
pub const V5_BASE_SPEC_SHA256: &str =
    "6bc0adfa60b827b8babbdc17713f2b5e2909a8898ccd0242e0806180ab786e43";
pub const V5_APPROVED_ADDENDUM_PATH: &str = "SPEC-v5-ADDENDUM-01.md";
pub const V5_APPROVED_ADDENDUM_SHA256: &str =
    "bf0cb575a9ea20e2995164b3e34b696f1828b4e0870f198731cc7c9624c0c9e9";
pub const V5_APPROVED_DIRECTIVE_PATH: &str = "SPEC-v5-DIRECTIVE-01.md";
pub const V5_APPROVED_DIRECTIVE_SHA256: &str =
    "d3b01715cf277730de027ef66ec2ea66f304b5ff388ebc9cb138aedd0cc98405";
pub const V5_APPROVED_CONFORMANCE_CONTRACT_ID: &str =
    "identitybench.v5.normative-contract.addendum-01.directive-01.v1";
pub const V5_APPROVED_FULL_SPEC_PROFILE: &str = "approved_full_spec_v1";
pub const V5_APPROVED_FULL_SPEC_AUTHOR_ORIGIN: &str = "approved_full_spec_author_v1";

pub const V5_DRAFT5_SPEC_PATH: &str = "SPEC-v5.0-draft.5.md";
pub const V5_DRAFT5_SPEC_SHA256: &str =
    "08f8d8da64dffb3b1b5a5bb549348b9d48f2eccb192abe64fb3062a3c62ff104";
pub const V5_PROMOTED_DRAFT5_CONTRACT_ID: &str =
    "identitybench.v5.normative-contract.draft5-consensus.v1";
pub const V5_PROMOTED_DRAFT5_FULL_SPEC_PROFILE: &str = "promoted_draft5_full_spec_v1";
pub const V5_PROMOTED_DRAFT5_FULL_SPEC_AUTHOR_ORIGIN: &str =
    "fresh_promoted_draft5_full_spec_author_v1";
pub const V5_PROMOTED_DRAFT5_AGENT_VIEW_PROFILE: &str = "promoted_draft5_card_only_v1";
pub const V5_PROMOTED_DRAFT5_AGENT_VIEW_AUTHOR_ORIGIN: &str =
    "fresh_promoted_draft5_card_only_author_v1";
pub const V5_PROMOTED_DRAFT5_PRODUCTION_PROFILE: &str = "production_v5";

/// Path and SHA-256 reference for a normative specification document.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NormativeSpecRefV5 {
    pub path: String,
    pub sha256: String,
}

fn spec_ref_value(path: &str, sha256: &str) -> Value {
    let mut m = Map::new();
    m.insert("path".to_owned(), Value::String(path.to_owned()));
    m.insert("sha256".to_owned(), Value::String(sha256.to_owned()));
    Value::Object(m)
}

/// Return the exact metadata binding for the approved current v5 contract.
#[must_use]
pub fn approved_v5_normative_contract() -> Value {
    let mut m = Map::new();
    m.insert(
        "contract_id".to_owned(),
        Value::String(V5_APPROVED_CONFORMANCE_CONTRACT_ID.to_owned()),
    );
    m.insert(
        "base_spec".to_owned(),
        spec_ref_value(V5_BASE_SPEC_PATH, V5_BASE_SPEC_SHA256),
    );
    m.insert(
        "approved_addendum".to_owned(),
        spec_ref_value(V5_APPROVED_ADDENDUM_PATH, V5_APPROVED_ADDENDUM_SHA256),
    );
    m.insert(
        "approved_directive".to_owned(),
        spec_ref_value(V5_APPROVED_DIRECTIVE_PATH, V5_APPROVED_DIRECTIVE_SHA256),
    );
    Value::Object(m)
}

/// Return the 4-document metadata binding for the promoted draft.5 v5 contract.
#[must_use]
pub fn promoted_v5_normative_contract() -> Value {
    let mut m = Map::new();
    m.insert(
        "contract_id".to_owned(),
        Value::String(V5_PROMOTED_DRAFT5_CONTRACT_ID.to_owned()),
    );
    m.insert(
        "base_spec".to_owned(),
        spec_ref_value(V5_BASE_SPEC_PATH, V5_BASE_SPEC_SHA256),
    );
    m.insert(
        "protocol_revision_spec".to_owned(),
        spec_ref_value(V5_DRAFT5_SPEC_PATH, V5_DRAFT5_SPEC_SHA256),
    );
    m.insert(
        "approved_addendum".to_owned(),
        spec_ref_value(V5_APPROVED_ADDENDUM_PATH, V5_APPROVED_ADDENDUM_SHA256),
    );
    m.insert(
        "approved_directive".to_owned(),
        spec_ref_value(V5_APPROVED_DIRECTIVE_PATH, V5_APPROVED_DIRECTIVE_SHA256),
    );
    Value::Object(m)
}
