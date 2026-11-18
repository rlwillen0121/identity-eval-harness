//! Canonical v5 execution topology profiles and resolution rules (§4.2.1 & G-CAL-03).
//!
//! Mirrors `identitybench.v5.topologies`.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;

use indexmap::IndexMap;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::schemas::{SchemaValidationError, StrictV5Model};

pub const V5_ORDINARY_TOOLS: [&str; 4] = [
    "query_sources",
    "stage_phase_mutations",
    "request_cosign",
    "commit_phase_mutations",
];

pub const V5_DIAGNOSTIC_TOOLS: [&str; 2] = ["run_phase_checker", "request_team_steer"];

pub const V5_ALL_TOOLS: [&str; 6] = [
    "query_sources",
    "stage_phase_mutations",
    "request_cosign",
    "commit_phase_mutations",
    "run_phase_checker",
    "request_team_steer",
];

pub const LEGACY_TOPOLOGY_ALIASES_V5: [(&str, &str); 2] = [
    ("SINGLE_AGENT_UNIFIED", "T0_SINGLE_MONOLITH"),
    ("MULTI_AGENT_SPECIALIZED", "T5_HIERARCHICAL_PIPELINE"),
];

pub const CANONICAL_TOPOLOGIES_V5: [&str; 3] = [
    "T0_SINGLE_MONOLITH",
    "T3_MAKER_CHECKER_GUARDRAIL",
    "T5_HIERARCHICAL_PIPELINE",
];

pub const UNSUPPORTED_TOPOLOGIES_V5: [&str; 3] = [
    "T1_DOMAIN_SPECIALISTS",
    "T2_REVIEWER_COUNCIL",
    "T4_PARALLEL_SWARM_FENCED",
];

/// Encode JSON-serializable values in deterministic JCS-compatible UTF-8 bytes.
pub fn jcs_dumps_v5(value: &Value) -> Result<Vec<u8>, SchemaValidationError> {
    Ok(ib_core::jcs_dumps(value)?)
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CanonicalTopologyV5 {
    #[serde(rename = "T0_SINGLE_MONOLITH")]
    T0SingleMonolith,
    #[serde(rename = "T3_MAKER_CHECKER_GUARDRAIL")]
    T3MakerCheckerGuardrail,
    #[serde(rename = "T5_HIERARCHICAL_PIPELINE")]
    T5HierarchicalPipeline,
}

impl CanonicalTopologyV5 {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::T0SingleMonolith => "T0_SINGLE_MONOLITH",
            Self::T3MakerCheckerGuardrail => "T3_MAKER_CHECKER_GUARDRAIL",
            Self::T5HierarchicalPipeline => "T5_HIERARCHICAL_PIPELINE",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum CausalBarrierModeV5 {
    SynchronousDispatchRounds,
}

/// Role contract inside a frozen v5 execution topology profile.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TopologyRoleSpecV5 {
    pub role_id: String,
    pub role_priority: i64,
    pub allowed_tools: Vec<String>,
    #[serde(default)]
    pub uses_gated_semantic_checker: bool,
    #[serde(default = "default_true")]
    pub preserves_complete_transcript: bool,
}

impl StrictV5Model for TopologyRoleSpecV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        if self.role_id.is_empty() {
            return Err(SchemaValidationError::validation(
                "TopologyRoleSpecV5.role_id must be non-empty",
            ));
        }
        if self.role_priority < 0 {
            return Err(SchemaValidationError::validation(
                "TopologyRoleSpecV5.role_priority must be >= 0",
            ));
        }
        if !self.preserves_complete_transcript {
            return Err(SchemaValidationError::validation(
                "TopologyRoleSpecV5.preserves_complete_transcript must be true",
            ));
        }
        let unique: BTreeSet<&str> = self.allowed_tools.iter().map(String::as_str).collect();
        if unique.len() != self.allowed_tools.len() {
            return Err(SchemaValidationError::validation(format!(
                "role {:?} allowed_tools must not contain duplicates",
                self.role_id
            )));
        }
        let mut unknown: Vec<&str> = unique
            .iter()
            .copied()
            .filter(|t| !V5_ALL_TOOLS.contains(t))
            .collect();
        unknown.sort_unstable();
        if !unknown.is_empty() {
            return Err(SchemaValidationError::validation(format!(
                "role {:?} references unknown v5 tools: {unknown:?}",
                self.role_id
            )));
        }
        let has_checker = unique.contains("run_phase_checker");
        if self.uses_gated_semantic_checker != has_checker {
            return Err(SchemaValidationError::validation(format!(
                "role {:?} must set uses_gated_semantic_checker=True iff 'run_phase_checker' is in allowed_tools",
                self.role_id
            )));
        }
        Ok(())
    }
}

/// Compute canonical JCS SHA-256 over all TopologyProfileV5 fields except the digest itself.
pub fn compute_topology_profile_sha256_v5(
    profile_fields: &Map<String, Value>,
) -> Result<String, SchemaValidationError> {
    let mut payload = Map::new();
    for (k, v) in profile_fields {
        if k != "topology_profile_sha256" {
            payload.insert(k.clone(), v.clone());
        }
    }
    let bytes = jcs_dumps_v5(&Value::Object(payload))?;
    Ok(ib_core::hashing::sha256_hex(bytes))
}

/// Immutable, versioned, hash-pinned v5 topology execution profile (§4.2.1 & G-CAL-03).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TopologyProfileV5 {
    pub profile_id: String,
    pub profile_version: String,
    pub canonical_topology: CanonicalTopologyV5,
    #[serde(default)]
    pub legacy_plumbing_alias: Option<String>,
    pub topology_priority: i64,
    pub roles: Vec<TopologyRoleSpecV5>,
    pub routing_policy: String,
    #[serde(default = "default_causal_barrier_mode")]
    pub causal_barrier_mode: CausalBarrierModeV5,
    #[serde(default = "default_true")]
    pub shared_episode_generation_cap: bool,
    pub termination_policy: String,
    pub topology_profile_sha256: String,
}

fn default_causal_barrier_mode() -> CausalBarrierModeV5 {
    CausalBarrierModeV5::SynchronousDispatchRounds
}

impl TopologyProfileV5 {
    /// Return the `TopologyRoleSpecV5` for `role_id`.
    pub fn role_spec(&self, role_id: &str) -> Result<&TopologyRoleSpecV5, SchemaValidationError> {
        self.roles
            .iter()
            .find(|r| r.role_id == role_id)
            .ok_or_else(|| {
                SchemaValidationError::validation(format!(
                    "role {role_id:?} not found in topology profile {:?}",
                    self.profile_id
                ))
            })
    }

    /// Return mapping from `role_id` to frozen integer `role_priority` (in role declaration order).
    #[must_use]
    pub fn role_priority_map(&self) -> IndexMap<String, i64> {
        self.roles
            .iter()
            .map(|r| (r.role_id.clone(), r.role_priority))
            .collect()
    }

    /// Return canonical JSON representation of topology and role priorities.
    pub fn topology_role_priority_json(&self) -> Result<String, SchemaValidationError> {
        let mut roles_map = Map::new();
        for (k, v) in self.role_priority_map() {
            roles_map.insert(k, Value::Number(v.into()));
        }
        let mut outer = Map::new();
        outer.insert(
            "topology_priority".to_owned(),
            Value::Number(self.topology_priority.into()),
        );
        outer.insert("roles".to_owned(), Value::Object(roles_map));
        let bytes = jcs_dumps_v5(&Value::Object(outer))?;
        String::from_utf8(bytes)
            .map_err(|e| SchemaValidationError::validation(format!("UTF-8 error: {e}")))
    }

    /// Return permitted tool names for `role_id` under `steering_mode`.
    pub fn permitted_tools_for_role(
        &self,
        role_id: &str,
        steering_mode: &str,
    ) -> Result<BTreeSet<String>, SchemaValidationError> {
        let spec = self.role_spec(role_id)?;
        let mut allowed: BTreeSet<String> = spec.allowed_tools.iter().cloned().collect();
        if self.canonical_topology != CanonicalTopologyV5::T5HierarchicalPipeline
            && steering_mode != "UNSTEERED"
            && !spec.allowed_tools.is_empty()
        {
            allowed.insert("request_team_steer".to_owned());
        }
        Ok(allowed)
    }
}

impl StrictV5Model for TopologyProfileV5 {
    fn validate(&self) -> Result<(), SchemaValidationError> {
        if self.profile_id.is_empty()
            || self.profile_version.is_empty()
            || self.routing_policy.is_empty()
            || self.termination_policy.is_empty()
        {
            return Err(SchemaValidationError::validation(
                "TopologyProfileV5 string fields must be non-empty",
            ));
        }
        if self.topology_priority < 0 {
            return Err(SchemaValidationError::validation(
                "TopologyProfileV5.topology_priority must be >= 0",
            ));
        }
        if !self.shared_episode_generation_cap {
            return Err(SchemaValidationError::validation(
                "TopologyProfileV5.shared_episode_generation_cap must be true",
            ));
        }
        if self.roles.is_empty() {
            return Err(SchemaValidationError::validation(
                "topology profile must declare at least one role",
            ));
        }
        for role in &self.roles {
            role.validate()?;
        }
        let role_ids: BTreeSet<&str> = self.roles.iter().map(|r| r.role_id.as_str()).collect();
        if role_ids.len() != self.roles.len() {
            return Err(SchemaValidationError::validation(
                "topology role_id values must be unique",
            ));
        }
        let priorities: BTreeSet<i64> = self.roles.iter().map(|r| r.role_priority).collect();
        if priorities.len() != self.roles.len() {
            return Err(SchemaValidationError::validation(
                "topology role_priority values must be unique",
            ));
        }
        if let Some(alias) = &self.legacy_plumbing_alias {
            let mapped = LEGACY_TOPOLOGY_ALIASES_V5
                .iter()
                .find(|(k, _)| *k == alias.as_str())
                .map(|(_, v)| *v);
            if mapped != Some(self.canonical_topology.as_str()) {
                return Err(SchemaValidationError::validation(format!(
                    "legacy_plumbing_alias {alias:?} does not map to {:?}",
                    self.canonical_topology.as_str()
                )));
            }
        }

        let ordinary: Vec<&str> = V5_ORDINARY_TOOLS.to_vec();
        match self.canonical_topology {
            CanonicalTopologyV5::T0SingleMonolith => {
                if self.topology_priority != 0 || self.roles.len() != 1 {
                    return Err(SchemaValidationError::validation(
                        "T0_SINGLE_MONOLITH requires topology_priority=0 and 1 role",
                    ));
                }
                let sole = &self.roles[0];
                if sole.role_id != "monolith" || sole.role_priority != 0 {
                    return Err(SchemaValidationError::validation(
                        "T0_SINGLE_MONOLITH sole role must be 'monolith' with priority 0",
                    ));
                }
                let sole_tools: Vec<&str> = sole.allowed_tools.iter().map(String::as_str).collect();
                if sole_tools != ordinary || sole.uses_gated_semantic_checker {
                    return Err(SchemaValidationError::validation(
                        "T0_SINGLE_MONOLITH must expose only the 4 ordinary tools with no checker",
                    ));
                }
            }
            CanonicalTopologyV5::T5HierarchicalPipeline => {
                let role_names: Vec<&str> = self.roles.iter().map(|r| r.role_id.as_str()).collect();
                if role_names.as_slice() != ["planner", "executor", "auditor"] {
                    return Err(SchemaValidationError::validation(
                        "T5_HIERARCHICAL_PIPELINE requires roles (planner, executor, auditor)",
                    ));
                }
                let (planner, executor, auditor) = (&self.roles[0], &self.roles[1], &self.roles[2]);
                if !(planner.role_priority < executor.role_priority
                    && executor.role_priority < auditor.role_priority)
                {
                    return Err(SchemaValidationError::validation(
                        "T5 role priorities must satisfy planner < executor < auditor",
                    ));
                }
                if !planner.allowed_tools.is_empty() || planner.uses_gated_semantic_checker {
                    return Err(SchemaValidationError::validation(
                        "T5 planner must have zero state-mutating/checker tools",
                    ));
                }
                let exec_tools: Vec<&str> =
                    executor.allowed_tools.iter().map(String::as_str).collect();
                if exec_tools != ordinary || executor.uses_gated_semantic_checker {
                    return Err(SchemaValidationError::validation(
                        "T5 executor must expose only the 4 ordinary tools",
                    ));
                }
                if !auditor.uses_gated_semantic_checker
                    || !auditor
                        .allowed_tools
                        .iter()
                        .any(|t| t == "run_phase_checker")
                {
                    return Err(SchemaValidationError::validation(
                        "T5 auditor must disclose uses_gated_semantic_checker=True",
                    ));
                }
            }
            CanonicalTopologyV5::T3MakerCheckerGuardrail => {}
        }

        let val = serde_json::to_value(self)?;
        let map = val
            .as_object()
            .ok_or_else(|| SchemaValidationError::validation("profile must serialize to object"))?;
        let expected_sha = compute_topology_profile_sha256_v5(map)?;
        if self.topology_profile_sha256 != expected_sha {
            return Err(SchemaValidationError::validation(
                "topology_profile_sha256 does not match canonical JCS profile digest",
            ));
        }
        Ok(())
    }
}

/// Construct an immutable `TopologyProfileV5` with its canonical JCS SHA-256 digest.
#[allow(clippy::too_many_arguments)]
pub fn build_topology_profile_v5(
    profile_id: impl Into<String>,
    profile_version: impl Into<String>,
    canonical_topology: CanonicalTopologyV5,
    legacy_plumbing_alias: Option<String>,
    topology_priority: i64,
    roles: Vec<TopologyRoleSpecV5>,
    routing_policy: impl Into<String>,
    termination_policy: impl Into<String>,
) -> Result<TopologyProfileV5, SchemaValidationError> {
    let profile_id = profile_id.into();
    let profile_version = profile_version.into();
    let routing_policy = routing_policy.into();
    let termination_policy = termination_policy.into();

    let mut temp = TopologyProfileV5 {
        profile_id,
        profile_version,
        canonical_topology,
        legacy_plumbing_alias,
        topology_priority,
        roles,
        routing_policy,
        causal_barrier_mode: CausalBarrierModeV5::SynchronousDispatchRounds,
        shared_episode_generation_cap: true,
        termination_policy,
        topology_profile_sha256: String::new(),
    };
    let val = serde_json::to_value(&temp)?;
    let map = val
        .as_object()
        .ok_or_else(|| SchemaValidationError::validation("profile must serialize to object"))?;
    temp.topology_profile_sha256 = compute_topology_profile_sha256_v5(map)?;
    temp.validate()?;
    Ok(temp)
}

pub static T0_UNIFIED_PROFILE_V5: LazyLock<TopologyProfileV5> = LazyLock::new(|| {
    build_topology_profile_v5(
        "identitybench.v5.topology.t0_single_monolith.v1",
        "1.0.0",
        CanonicalTopologyV5::T0SingleMonolith,
        Some("SINGLE_AGENT_UNIFIED".to_owned()),
        0,
        vec![TopologyRoleSpecV5 {
            role_id: "monolith".to_owned(),
            role_priority: 0,
            allowed_tools: V5_ORDINARY_TOOLS.iter().map(|s| (*s).to_owned()).collect(),
            uses_gated_semantic_checker: false,
            preserves_complete_transcript: true,
        }],
        "SINGLE_AGENT_DIRECT",
        "EARLY_TERMINAL_ON_EPISODE_COMPLETED_OR_WITHIN_PROTOCOL_STOP",
    )
    .expect("built-in T0_UNIFIED_PROFILE_V5 must be valid")
});

pub static T5_PLANNER_EXECUTOR_AUDITOR_PROFILE_V5: LazyLock<TopologyProfileV5> =
    LazyLock::new(|| {
        build_topology_profile_v5(
            "identitybench.v5.topology.t5_planner_executor_auditor.v1",
            "1.0.0",
            CanonicalTopologyV5::T5HierarchicalPipeline,
            Some("MULTI_AGENT_SPECIALIZED".to_owned()),
            1,
            vec![
                TopologyRoleSpecV5 {
                    role_id: "planner".to_owned(),
                    role_priority: 0,
                    allowed_tools: Vec::new(),
                    uses_gated_semantic_checker: false,
                    preserves_complete_transcript: true,
                },
                TopologyRoleSpecV5 {
                    role_id: "executor".to_owned(),
                    role_priority: 1,
                    allowed_tools: V5_ORDINARY_TOOLS.iter().map(|s| (*s).to_owned()).collect(),
                    uses_gated_semantic_checker: false,
                    preserves_complete_transcript: true,
                },
                TopologyRoleSpecV5 {
                    role_id: "auditor".to_owned(),
                    role_priority: 2,
                    allowed_tools: vec!["run_phase_checker".to_owned()],
                    uses_gated_semantic_checker: true,
                    preserves_complete_transcript: true,
                },
            ],
            "PLANNER_EXECUTOR_AUDITOR_STAGED_APPROVAL_PIPELINE",
            "EARLY_TERMINAL_ON_EPISODE_COMPLETED_OR_WITHIN_PROTOCOL_STOP",
        )
        .expect("built-in T5_PLANNER_EXECUTOR_AUDITOR_PROFILE_V5 must be valid")
    });

/// Resolve legacy plumbing alias to its canonical v5 topology ID.
#[must_use]
pub fn canonical_topology_id_v5(topology: &str) -> &str {
    LEGACY_TOPOLOGY_ALIASES_V5
        .iter()
        .find(|(alias, _)| *alias == topology)
        .map_or(topology, |(_, canonical)| *canonical)
}

/// Resolve and validate the executable `TopologyProfileV5` for `topology`.
pub fn resolve_topology_profile_v5(
    topology: &str,
    explicit_profile: Option<&TopologyProfileV5>,
) -> Result<TopologyProfileV5, SchemaValidationError> {
    let canonical = canonical_topology_id_v5(topology);
    if canonical.starts_with("T1_")
        || canonical.starts_with("T2_")
        || canonical.starts_with("T4_")
        || UNSUPPORTED_TOPOLOGIES_V5.contains(&canonical)
    {
        return Err(SchemaValidationError::validation(format!(
            "unsupported v5 topology {topology:?}; no execution profile is defined"
        )));
    }
    if !CANONICAL_TOPOLOGIES_V5.contains(&canonical) {
        return Err(SchemaValidationError::validation(format!(
            "unknown v5 topology {topology:?}"
        )));
    }

    if canonical == "T3_MAKER_CHECKER_GUARDRAIL" {
        let profile = explicit_profile.ok_or_else(|| {
            SchemaValidationError::validation(
                "T3_MAKER_CHECKER_GUARDRAIL requires an explicit reviewed TopologyProfileV5",
            )
        })?;
        if profile.canonical_topology != CanonicalTopologyV5::T3MakerCheckerGuardrail {
            return Err(SchemaValidationError::validation(format!(
                "explicit_profile topology {:?} does not match T3_MAKER_CHECKER_GUARDRAIL",
                profile.canonical_topology.as_str()
            )));
        }
        profile.validate()?;
        return Ok(profile.clone());
    }

    if canonical == "T0_SINGLE_MONOLITH" {
        if let Some(profile) = explicit_profile {
            if profile.canonical_topology != CanonicalTopologyV5::T0SingleMonolith {
                return Err(SchemaValidationError::validation(format!(
                    "explicit_profile topology {:?} does not match T0_SINGLE_MONOLITH",
                    profile.canonical_topology.as_str()
                )));
            }
            profile.validate()?;
            return Ok(profile.clone());
        }
        return Ok(T0_UNIFIED_PROFILE_V5.clone());
    }

    if let Some(profile) = explicit_profile {
        if profile.canonical_topology != CanonicalTopologyV5::T5HierarchicalPipeline {
            return Err(SchemaValidationError::validation(format!(
                "explicit_profile topology {:?} does not match T5_HIERARCHICAL_PIPELINE",
                profile.canonical_topology.as_str()
            )));
        }
        profile.validate()?;
        return Ok(profile.clone());
    }
    Ok(T5_PLANNER_EXECUTOR_AUDITOR_PROFILE_V5.clone())
}

// Keep BTreeMap in scope for any callers needing ordered priority map helpers.
#[allow(dead_code)]
fn _role_priority_btreemap(profile: &TopologyProfileV5) -> BTreeMap<String, i64> {
    profile
        .roles
        .iter()
        .map(|r| (r.role_id.clone(), r.role_priority))
        .collect()
}
