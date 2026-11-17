use std::collections::BTreeSet;

use ib_v5_runtime::{
    LogicalInput, RULE_TO_POLICY_CARD_ANCHORS_V5, count_logical_episode_tokens,
    count_policy_card_tokens, policy_card_sections, render_policy_card, render_rules_markdown,
    validate_logical_episode_tokens, validate_policy_card, validate_policy_card_coverage,
};
use ib_v5_schemas::{
    ConnectorUnmappedPolicyDefaultV5, PolicyCardV5, PolicyOverlayV5, PrivilegeTierV5,
    ScopeCatalogV5, ScopedSoDRuleV5, SoDScopeModeV5,
};
use serde_json::{Value, json};

fn sample_overlay() -> PolicyOverlayV5 {
    PolicyOverlayV5 {
        freshness_sla_s: 3600,
        rotation_sla_s: 3600,
        cae_sla_s: 600,
        conversion_grace_s: 3600,
        break_glass_cooldown_s: 3600,
        allow_contractor_nhi_owners: false,
        allow_contractor_agent_sponsors: false,
        enforce_cross_nhi_owner_sod: true,
        enforce_sponsor_agent_sod: true,
        permit_hitl_step_up_for_autonomous: false,
        require_immutable_hr_correlation: true,
        connector_unmapped_policy_default: ConnectorUnmappedPolicyDefaultV5::HaltConnector,
        uar_preserve_standing_floor_default: true,
        owner_excluded_dept_prefixes: Vec::new(),
        max_prod_nhis_per_owner: 5,
        max_prod_agents_per_sponsor: 5,
        max_agent_delegation_hops: 2,
        critical_jit_tiers: vec![PrivilegeTierV5::Admin, PrivilegeTierV5::Critical],
        jit_ttl_cap_by_tier: vec![
            (PrivilegeTierV5::Read, 3600),
            (PrivilegeTierV5::Standard, 3600),
            (PrivilegeTierV5::Elevated, 1800),
            (PrivilegeTierV5::Admin, 900),
            (PrivilegeTierV5::Critical, 300),
        ],
        forbidden_sod_rules: vec![ScopedSoDRuleV5 {
            rule_code: "SOD_TEST".to_owned(),
            cap_a: "approve".to_owned(),
            cap_b: "pay".to_owned(),
            scope_mode: SoDScopeModeV5::Global,
        }],
        temporal_sod_rules: Vec::new(),
    }
}

fn sample_card() -> PolicyCardV5 {
    render_policy_card(
        &ScopeCatalogV5 {
            leaf_scopes: vec!["/org/engineering".to_owned()],
        },
        &sample_overlay(),
    )
    .expect("sample card")
}

#[test]
fn policy_card_matches_authored_anchors_for_every_rule_and_boundary() {
    let card = sample_card();
    assert_eq!(RULE_TO_POLICY_CARD_ANCHORS_V5.len(), 39);
    assert!(validate_policy_card_coverage(&card.rendered_rules_markdown).is_empty());

    let damaged = card
        .rendered_rules_markdown
        .replace("own=min(nonnull ack,expiry)", "own=min(ack,expiry)");
    let errors = validate_policy_card_coverage(&damaged);
    assert!(errors.iter().any(|e| e.contains("CAE-V5-01 differs")));

    let missing_section = card
        .rendered_rules_markdown
        .replace("### AGENT-DEL-V5-01.D4", "### Omitted");
    assert!(
        validate_policy_card_coverage(&missing_section)
            .iter()
            .any(|e| e.contains("AGENT-DEL-V5-01.D4"))
    );

    let duplicated = format!(
        "{}\n### CAE-V5-01\nduplicate\n",
        card.rendered_rules_markdown
    );
    assert!(
        validate_policy_card_coverage(&duplicated)
            .iter()
            .any(|e| e.contains("duplicate section CAE-V5-01"))
    );
}

#[test]
fn rendered_anchor_index_expands_to_complete_public_map() {
    let sections = policy_card_sections(&render_rules_markdown());
    let tools = &sections["TOOLS"];
    let line = tools
        .lines()
        .find(|l| l.starts_with("ANCHOR_INDEX_V5="))
        .expect("ANCHOR_INDEX_V5 line");
    let after_eq = line.strip_prefix("ANCHOR_INDEX_V5=").unwrap();
    let (body, _) = after_eq.split_once("}.").unwrap();
    let json_str = format!("{body}}}");
    let index: Value = serde_json::from_str(&json_str).expect("parse anchor index");

    let rules = index["rules"].as_array().expect("rules array");
    let boundaries = index["boundaries"].as_array().expect("boundaries array");
    let aliases = index["aliases"].as_object().expect("aliases object");
    assert_eq!(rules.len(), 26);
    assert_eq!(boundaries.len(), 8);
    assert_eq!(aliases.len(), 5);

    let mut expanded = indexmap::IndexMap::new();
    for r in rules {
        let s = r.as_str().unwrap().to_owned();
        expanded.insert(s.clone(), vec![s]);
    }
    for b in boundaries {
        let s = b.as_str().unwrap().to_owned();
        expanded.insert(format!("BOUNDARY:{s}"), vec![s]);
    }
    for (name, section) in aliases {
        let s = section.as_str().unwrap().to_owned();
        expanded.insert(format!("BOUNDARY:{name}"), vec![s]);
    }
    assert_eq!(expanded, *RULE_TO_POLICY_CARD_ANCHORS_V5);
}

#[test]
fn policy_card_validator_allows_large_cards_and_rejects_truncation() {
    let card = sample_card();
    assert!(validate_policy_card(&card).is_empty());

    let mut large_card = card.clone();
    large_card.scope_catalog = ScopeCatalogV5 {
        leaf_scopes: (0..600).map(|i| format!("/org/dept_{i:04}")).collect(),
    };
    assert!(count_policy_card_tokens(&large_card).unwrap() > 3_200);
    assert!(validate_policy_card(&large_card).is_empty());

    let mut mutated = card.clone();
    mutated.rendered_rules_markdown =
        format!("{}{}", card.rendered_rules_markdown, " x".repeat(4_000));
    let errors = validate_policy_card(&mutated);
    assert!(
        errors
            .iter()
            .any(|e| e.contains("section") || e.contains("canonical"))
    );

    let mut truncated = card.clone();
    truncated.rendered_rules_markdown = card.rendered_rules_markdown[..2_000].to_owned();
    assert!(!validate_policy_card(&truncated).is_empty());
}

#[test]
fn logical_input_counter_has_no_9600_cap_and_validates_parts() {
    let json_payload = json!({"slice": "payload"});
    let tiny = count_logical_episode_tokens(&[
        LogicalInput::Str("system"),
        LogicalInput::Json(&json_payload),
    ])
    .unwrap();
    assert!(tiny > 0);
    assert!(
        validate_logical_episode_tokens(
            &[
                LogicalInput::Str("system"),
                LogicalInput::Json(&json_payload)
            ],
            None
        )
        .unwrap()
        .is_empty()
    );

    let large_payload = "x ".repeat(12_000);
    assert!(count_logical_episode_tokens(&[LogicalInput::Str(&large_payload)]).unwrap() > 9_600);
    assert!(
        validate_logical_episode_tokens(&[LogicalInput::Str(&large_payload)], None)
            .unwrap()
            .is_empty()
    );
    let card = sample_card();
    assert!(
        validate_logical_episode_tokens(
            &[
                LogicalInput::Str("system"),
                LogicalInput::PolicyCard(&card),
                LogicalInput::Str(&large_payload),
            ],
            None,
        )
        .unwrap()
        .is_empty()
    );

    assert!(
        !validate_logical_episode_tokens(&[], None)
            .unwrap()
            .is_empty()
    );
    assert!(
        !validate_logical_episode_tokens(&[LogicalInput::Str("")], None)
            .unwrap()
            .is_empty()
    );
    assert!(
        !validate_logical_episode_tokens(&[LogicalInput::None], None)
            .unwrap()
            .is_empty()
    );

    let mut truncated_card = card.clone();
    truncated_card.rendered_rules_markdown = "### TOOLS\ntruncated".to_owned();
    assert!(
        !validate_logical_episode_tokens(
            &[
                LogicalInput::Str("system"),
                LogicalInput::PolicyCard(&truncated_card)
            ],
            None
        )
        .unwrap()
        .is_empty()
    );
    let over_limit = "x ".repeat(100);
    assert!(
        !validate_logical_episode_tokens(&[LogicalInput::Str(&over_limit)], Some(50))
            .unwrap()
            .is_empty()
    );
}

#[test]
fn rules_markdown_contains_all_literal_rule_ids_and_no_obsolete_ceilings() {
    let md = render_rules_markdown();
    assert_eq!(md, render_rules_markdown());
    for key in RULE_TO_POLICY_CARD_ANCHORS_V5.keys() {
        if !key.starts_with("BOUNDARY:") {
            assert!(md.contains(&format!("### {key}")));
        }
    }
    for obsolete in [
        "3200",
        "3,200",
        "9600",
        "9,600",
        "max turns=8",
        "10 for T3 or steered modes",
        "14500",
        "27500",
        "10500",
        "23500",
        "1450000",
        "cost_ceiling_usd",
        "$12",
        "PASS_15X",
        "FAIL_15X",
        "15x",
        "OUTPUT_BUDGET_STOP",
        "G-SRR@episode-envelope",
        "max_output_tokens=8192",
    ] {
        assert!(!md.contains(obsolete), "found obsolete text {obsolete:?}");
    }
    let sections = policy_card_sections(&md);
    let stage5 = &sections["STAGE5"];
    let all_categories: BTreeSet<&str> = [
        "COMPLETENESS_VIOLATION",
        "SCHEMA_VALIDATION_ERROR",
        "PRECONDITION_MISMATCH",
        "CITATION_MISMATCH",
        "CONNECTOR_HALT_OR_FREEZE",
        "CONNECTOR_RECONCILIATION_SET",
        "SPOKE_DRIFT_REMEDIATION",
        "ITSM_MANUAL_FULFILLMENT_MISMATCH",
        "HR_FEED_AUTHORITY_MISMATCH",
        "HR_CORRELATION_OR_PRECEDENCE_MISMATCH",
        "BINDING_OR_ENGAGEMENT_CONFLICT",
        "CONV_GRACE_BREACH",
        "COMPENSATING_CONTROL_INVALID",
        "MOVER_OR_SCOPED_SELF_SOD",
        "STALE_TELEMETRY_OR_ROTATION_SLA",
        "NHI_OVERLAY_ELIGIBILITY_OR_CAP",
        "NHI_OWNER_PRECEDENCE",
        "AGENT_ATTESTATION_OR_STALE_DIGEST",
        "AGENT_SPONSOR_ELIGIBILITY_OR_CAP",
        "AGENT_SCOPE_TIERING_OR_SOD",
        "CIEM_ORPHAN_PRINCIPAL_MISMATCH",
        "CIEM_ESCALATION_PATH_MISMATCH",
        "CIEM_EFFECTIVE_PERMISSION_OR_REMEDIATION",
        "OPTIMIZATION_INFEASIBLE",
        "OPTIMIZATION_SUBOPTIMAL",
        "JML_BIRTHRIGHT_ZSP_OR_SOD_BREACH",
        "JML_WORKFLOW_SPEC_UNSAFE",
        "RETRO_APPROVAL_TAINT_MISMATCH",
        "CAE_REVOCATION_DECISION",
        "CAE_REFRESH_TREE_EXPOSURE",
        "UAR_BASELINE_OR_SHOCK_FILTER",
        "UAR_REVIEWER_COI_OVERRIDE",
        "UAR_INHERITED_STANDING_LOCK",
        "QUORUM_OR_CUSTODIAN_DEPLETION",
        "JIT_REQUESTER_OR_SCOPE_INELIGIBLE",
        "TICKET_STALE_OR_RETRO_TAINTED",
        "SCOPED_OR_BREAKGLASS_SOD_COOLDOWN",
        "APPROVER_DISQUALIFIED",
        "JIT_DUAL_CONTROL_OR_TTL_BOUND",
        "AGENT_DELEGATION_PRINCIPAL_INVALID",
        "AGENT_OBO_INTERSECTION_OR_HOP_BREACH",
        "AGENT_WORKFLOW_OR_SESSION_SOD",
        "AGENT_HITL_COSIGN_OR_TTL_MISMATCH",
    ]
    .into_iter()
    .collect();
    for cat in all_categories {
        assert!(
            stage5.contains(&format!("`{cat}`")),
            "missing {cat} in STAGE5"
        );
    }
}
