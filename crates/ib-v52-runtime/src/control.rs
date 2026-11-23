//! §8.2 live validity-control identities for v5.2 calibration runs.

use std::str::FromStr;

use serde::{Deserialize, Serialize};

/// A calibration control that disables access to selected workspace inputs or tools.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunControlV52 {
    BlindNoArtifacts,
    BlindNoProbing,
}

impl RunControlV52 {
    /// The CLI and manifest spelling for this control.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::BlindNoArtifacts => "blind_no_artifacts",
            Self::BlindNoProbing => "blind_no_probing",
        }
    }
}

impl FromStr for RunControlV52 {
    type Err = &'static str;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "blind_no_artifacts" => Ok(Self::BlindNoArtifacts),
            "blind_no_probing" => Ok(Self::BlindNoProbing),
            _ => Err("expected blind_no_artifacts or blind_no_probing"),
        }
    }
}
