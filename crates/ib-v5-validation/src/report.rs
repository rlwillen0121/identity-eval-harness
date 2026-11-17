//! Validation report and gate types shared by the v5 acceptance checks.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

const LIVE_EVIDENCE_GATES: [&str; 4] = ["G-ECON-01", "G-CAL-01", "G-CAL-02", "G-CAL-03"];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum GateStatusV5 {
    #[serde(rename = "PASS")]
    Pass,
    #[serde(rename = "FAIL")]
    Fail,
    #[serde(rename = "UNKNOWN")]
    Unknown,
}

impl GateStatusV5 {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Pass => "PASS",
            Self::Fail => "FAIL",
            Self::Unknown => "UNKNOWN",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GateResultV5 {
    pub gate_id: String,
    pub status: GateStatusV5,
    #[serde(default)]
    pub evidence: Map<String, Value>,
    #[serde(default)]
    pub errors: Vec<String>,
}

impl GateResultV5 {
    #[must_use]
    pub fn new(
        gate_id: impl Into<String>,
        status: GateStatusV5,
        evidence: Map<String, Value>,
        errors: Vec<String>,
    ) -> Self {
        Self {
            gate_id: gate_id.into(),
            status,
            evidence,
            errors,
        }
    }

    #[must_use]
    pub fn with_evidence(
        gate_id: impl Into<String>,
        status: GateStatusV5,
        evidence: Value,
        errors: Vec<String>,
    ) -> Self {
        Self::new(
            gate_id,
            status,
            evidence.as_object().cloned().unwrap_or_default(),
            errors,
        )
    }

    #[must_use]
    pub fn to_mapping(&self) -> Value {
        json!({
            "gate_id": self.gate_id,
            "status": self.status.as_str(),
            "evidence": self.evidence,
            "errors": self.errors,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct V5ValidationReport {
    pub suite_id: String,
    pub dataset_dir: String,
    pub episode_count: usize,
    pub gate_results: Vec<GateResultV5>,
}

impl V5ValidationReport {
    /// Whether all offline dataset and harness gates passed.
    ///
    /// Live economics and calibration are reported independently; this property does not
    /// claim that the full M5 milestone passed.
    #[must_use]
    pub fn passed(&self) -> bool {
        self.gate_results.iter().all(|item| {
            LIVE_EVIDENCE_GATES.contains(&item.gate_id.as_str())
                || item.status == GateStatusV5::Pass
        })
    }

    #[must_use]
    pub fn full_milestone_passed(&self) -> bool {
        self.gate_results
            .iter()
            .all(|item| item.status == GateStatusV5::Pass)
    }

    #[must_use]
    pub fn live_gates_unmeasured(&self) -> Vec<String> {
        self.gate_results
            .iter()
            .filter(|item| {
                LIVE_EVIDENCE_GATES.contains(&item.gate_id.as_str())
                    && item.status == GateStatusV5::Unknown
            })
            .map(|item| item.gate_id.clone())
            .collect()
    }

    #[must_use]
    pub fn errors(&self) -> Vec<String> {
        self.gate_results
            .iter()
            .flat_map(|item| {
                item.errors
                    .iter()
                    .map(|error| format!("{}: {error}", item.gate_id))
            })
            .collect()
    }

    #[must_use]
    pub fn unknown_gates(&self) -> Vec<String> {
        self.gate_results
            .iter()
            .filter(|item| item.status == GateStatusV5::Unknown)
            .map(|item| item.gate_id.clone())
            .collect()
    }

    #[must_use]
    pub fn to_mapping(&self) -> Value {
        json!({
            "suite_id": self.suite_id,
            "dataset_dir": self.dataset_dir,
            "episode_count": self.episode_count,
            "passed": self.passed(),
            "offline_passed": self.passed(),
            "full_milestone_passed": self.full_milestone_passed(),
            "live_gates_unmeasured": self.live_gates_unmeasured(),
            "gate_results": self.gate_results.iter().map(GateResultV5::to_mapping).collect::<Vec<_>>(),
            "errors": self.errors(),
            "unknown_gates": self.unknown_gates(),
        })
    }
}
