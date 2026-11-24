//! Mechanism E UAR closure tables: remediation tickets, readback watermarks, signed role bundles.

use indexmap::IndexMap;

use super::WorldCtx;
use super::dates::prev_day_iso;
use super::group_ids::RoleGroupIds;
use super::personas::Personas;
use super::types::{SignedRoleBundleEntryV52, SpokeReadbackWatermarkV52, UarRemediationTicketV52};

/// Mechanism E (`F08`/`F10`/`F11`) side tables.
pub(super) struct UarClosureTables {
    pub(super) uar_remediation_ticket_log: Vec<UarRemediationTicketV52>,
    pub(super) spoke_readback_watermarks: Vec<SpokeReadbackWatermarkV52>,
    pub(super) signed_role_bundle_matrix: Vec<SignedRoleBundleEntryV52>,
}

/// Mechanism E: remediation ticket log, spoke readback watermarks & signed role bundle matrix.
pub(super) fn build_uar_closure_tables(
    ctx: &WorldCtx<'_>,
    has_uar_mech_e: bool,
    uar_ids: &[String],
    ad_sam_by_person: &IndexMap<String, String>,
) -> UarClosureTables {
    let WorldCtx { client, .. } = *ctx;
    let Personas {
        ceo_vp,
        phantom_close_requester,
        adm_fin_uar_dual,
        uar_exp_exc_user,
        ..
    } = ctx.personas.clone();
    let RoleGroupIds {
        req_clean_b,
        req_clean_c,
        uar_core_vault,
        uar_shared_collab,
        uar_split_child,
        uar_dept_bundle_locked,
        uar_exc_temp,
        uar_priv_audit,
        fin_group,
        it_group,
        leg_group,
        ..
    } = ctx.group_ids.clone();
    // Mechanism E (`F08`/`F10`/`F11`): Remediation ticket log, spoke readback watermarks & signed role bundle matrix.
    let mut uar_remediation_ticket_log: Vec<UarRemediationTicketV52> = Vec::new();
    let mut spoke_readback_watermarks: Vec<SpokeReadbackWatermarkV52> = Vec::new();
    let mut signed_role_bundle_matrix: Vec<SignedRoleBundleEntryV52> = Vec::new();
    if has_uar_mech_e {
        let d_m2 = prev_day_iso(&client.cutover_date, 2);
        let d_m3 = prev_day_iso(&client.cutover_date, 3);
        let watermark_ts = format!("{}T06:00:00Z", client.cutover_date);
        spoke_readback_watermarks = vec![
            SpokeReadbackWatermarkV52 {
                spoke_id: "ad".to_owned(),
                last_complete_aggregation_at: watermark_ts.clone(),
            },
            SpokeReadbackWatermarkV52 {
                spoke_id: "custom_rest".to_owned(),
                last_complete_aggregation_at: watermark_ts.clone(),
            },
            SpokeReadbackWatermarkV52 {
                spoke_id: "entra".to_owned(),
                last_complete_aggregation_at: watermark_ts,
            },
        ];

        let phantom_adm_sam = ad_sam_by_person
            .get(&phantom_close_requester)
            .map(|s| format!("adm_{s}"))
            .unwrap_or_else(|| "adm_unknown".to_owned());
        let exp_exc_sam = ad_sam_by_person
            .get(&uar_exp_exc_user)
            .cloned()
            .unwrap_or_default();
        let fin_dual_sam = ad_sam_by_person
            .get(&adm_fin_uar_dual)
            .cloned()
            .unwrap_or_default();

        uar_remediation_ticket_log = vec![
            // True unremediated closed ticket: CLOSED_COMPLETE, closed_at < custom_rest watermark <= cutover,
            // AND req_clean_b still appears on usr_<ceo_vp> in spoke_accounts.json!
            UarRemediationTicketV52 {
                ticket_id: "INC-88412".to_owned(),
                item_id: uar_ids[4].clone(),
                spoke_id: "custom_rest".to_owned(),
                account_id: format!("r{}", &ceo_vp[1..]),
                entitlement_id: req_clean_b.clone(),
                status: "CLOSED_COMPLETE".to_owned(),
                closed_at: Some(format!("{d_m2}T10:15:00Z")),
            },
            // Control decoy #1: OPEN_IN_PROGRESS (not closed!) -> must NOT trigger reopen_unremediated_spoke_target!
            UarRemediationTicketV52 {
                ticket_id: "INC-88419".to_owned(),
                item_id: uar_ids[1].clone(),
                spoke_id: "ad".to_owned(),
                account_id: phantom_adm_sam,
                entitlement_id: uar_priv_audit,
                status: "OPEN_IN_PROGRESS".to_owned(),
                closed_at: None,
            },
            // Control decoy #2: CLOSED_COMPLETE but closed_at (18:00Z) is AFTER ad watermark (06:00Z) -> no readback after closure!
            UarRemediationTicketV52 {
                ticket_id: "INC-88425".to_owned(),
                item_id: uar_ids[2].clone(),
                spoke_id: "ad".to_owned(),
                account_id: exp_exc_sam,
                entitlement_id: uar_exc_temp,
                status: "CLOSED_COMPLETE".to_owned(),
                closed_at: Some(format!("{}T18:00:00Z", client.cutover_date)),
            },
            // Control decoy #3: CLOSED_COMPLETE before watermark, but entitlement already absent from account!
            UarRemediationTicketV52 {
                ticket_id: "INC-88390".to_owned(),
                item_id: uar_ids[6].clone(),
                spoke_id: "ad".to_owned(),
                account_id: fin_dual_sam,
                entitlement_id: req_clean_c.clone(),
                status: "CLOSED_COMPLETE".to_owned(),
                closed_at: Some(format!("{d_m3}T11:00:00Z")),
            },
        ];

        // Signed job-bundle matrix keyed by coalesced HR (department, job_code):
        // - (Finance, FIN_TREASURY) requires uar_shared_collab + uar_core_vault (locks uar_shared_collab for colleague fallback_user_2!)
        // - (Finance, FIN_ANALYST) does NOT require uar_shared_collab (so adm_fin_uar_dual -> revoke_all_subject_paths!),
        //   while justifying fin_group (so uar_ids[6] -> certify!)
        // - (Engineering, DIR_ENG) requires uar_dept_bundle_locked + uar_split_child (locks uar_dept_bundle_locked for active_dir_1!)
        // - (Engineering, ENG_SR) requires uar_dept_bundle_locked WITHOUT uar_split_child (so fallback_user_1 -> route_nested_group_owner_split!)
        signed_role_bundle_matrix = vec![
            SignedRoleBundleEntryV52 {
                department: "Finance".to_owned(),
                job_code: "FIN_TREASURY".to_owned(),
                required_shared_group_id: Some(uar_shared_collab),
                justified_entitlement_ids: vec![fin_group.clone(), uar_core_vault],
            },
            SignedRoleBundleEntryV52 {
                department: "Finance".to_owned(),
                job_code: "FIN_ANALYST".to_owned(),
                required_shared_group_id: None,
                justified_entitlement_ids: vec![fin_group],
            },
            SignedRoleBundleEntryV52 {
                department: "Finance".to_owned(),
                job_code: "EXEC_VP".to_owned(),
                required_shared_group_id: None,
                justified_entitlement_ids: vec![],
            },
            SignedRoleBundleEntryV52 {
                department: "Engineering".to_owned(),
                job_code: "DIR_ENG".to_owned(),
                required_shared_group_id: Some(uar_dept_bundle_locked.clone()),
                justified_entitlement_ids: vec![uar_dept_bundle_locked.clone(), uar_split_child],
            },
            SignedRoleBundleEntryV52 {
                department: "Engineering".to_owned(),
                job_code: "ENG_SR".to_owned(),
                required_shared_group_id: Some(uar_dept_bundle_locked.clone()),
                justified_entitlement_ids: vec![uar_dept_bundle_locked],
            },
            SignedRoleBundleEntryV52 {
                department: "Information Technology".to_owned(),
                job_code: "IT_OPS".to_owned(),
                required_shared_group_id: None,
                justified_entitlement_ids: vec![it_group],
            },
            SignedRoleBundleEntryV52 {
                department: "Legal".to_owned(),
                job_code: "LEG_COUNSEL".to_owned(),
                required_shared_group_id: None,
                justified_entitlement_ids: vec![leg_group],
            },
        ];
    }

    UarClosureTables {
        uar_remediation_ticket_log,
        spoke_readback_watermarks,
        signed_role_bundle_matrix,
    }
}
