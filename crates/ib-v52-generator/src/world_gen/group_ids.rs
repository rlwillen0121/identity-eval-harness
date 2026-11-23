//! Group IDs resolved by semantic role tag from the generated group catalog.

use super::types::{GroupBlueprintV52, GroupRoleTagV52};

/// Group IDs (and near-threshold decoy `(group_id, spoke_id)` sets) keyed by role tag.
#[derive(Debug, Clone)]
pub(super) struct RoleGroupIds {
    pub(super) sod_ap_left_group: String,
    pub(super) sod_vendor_right_group: String,
    pub(super) sod_deploy_left_group: String,
    pub(super) sod_code_right_group: String,
    pub(super) priv_admin_group: String,
    pub(super) req_clean_a: String,
    pub(super) req_clean_b: String,
    pub(super) req_clean_c: String,
    pub(super) uar_core_vault: String,
    pub(super) uar_shared_collab: String,
    pub(super) uar_adm_bridge: String,
    pub(super) uar_split_child: String,
    pub(super) uar_dept_bundle_locked: String,
    pub(super) uar_exc_temp: String,
    pub(super) uar_exc_valid: String,
    pub(super) uar_priv_audit: String,
    pub(super) nested_eng_squad: String,
    pub(super) nested_fin_squad: String,
    pub(super) nested_adm_sod_left_group: String,
    pub(super) all_emp_group: String,
    pub(super) all_emp_entra_group: String,
    pub(super) fin_group: String,
    pub(super) fin_spoke_group: String,
    pub(super) eng_group: String,
    pub(super) eng_spoke_group: String,
    pub(super) it_group: String,
    pub(super) it_spoke_group: String,
    pub(super) clin_group: String,
    pub(super) acad_group: String,
    pub(super) leg_group: String,
    pub(super) stu_group: String,
    pub(super) cwk_group: String,
    pub(super) nonprod_group: String,
    pub(super) stage_eng_twin: String,
    pub(super) uat_it_twin: String,
    pub(super) decoys_all_emp: Vec<(String, String)>,
    pub(super) decoys_fin: Vec<(String, String)>,
    pub(super) decoys_eng: Vec<(String, String)>,
    pub(super) decoys_it: Vec<(String, String)>,
    pub(super) decoys_clin: Vec<(String, String)>,
    pub(super) decoys_acad: Vec<(String, String)>,
}

/// Resolve every role-tagged group ID used by the persona, entitlement, and Day-2 builders.
pub(super) fn resolve_role_group_ids(groups: &[GroupBlueprintV52]) -> RoleGroupIds {
    let find_group_by_tag = |tag: GroupRoleTagV52| -> String {
        groups
            .iter()
            .find(|g| g.role_tag == tag)
            .map(|g| g.record.group_id.clone())
            .unwrap_or_default()
    };

    let sod_ap_left_group = find_group_by_tag(GroupRoleTagV52::SodApPostLeft);
    let sod_vendor_right_group = find_group_by_tag(GroupRoleTagV52::SodVendorMaintRight);
    let sod_deploy_left_group = find_group_by_tag(GroupRoleTagV52::SodDeployApproveLeft);
    let sod_code_right_group = find_group_by_tag(GroupRoleTagV52::SodCodeCommitRight);
    let priv_admin_group = find_group_by_tag(GroupRoleTagV52::PrivilegedAdmin);
    let req_clean_a = find_group_by_tag(GroupRoleTagV52::RequestableAppCleanA);
    let req_clean_b = find_group_by_tag(GroupRoleTagV52::RequestableAppCleanB);
    let req_clean_c = find_group_by_tag(GroupRoleTagV52::RequestableAppCleanC);
    let uar_core_vault = find_group_by_tag(GroupRoleTagV52::UarCoreVaultTarget);
    let uar_shared_collab = find_group_by_tag(GroupRoleTagV52::UarSharedCollab);
    let uar_adm_bridge = find_group_by_tag(GroupRoleTagV52::UarAdmBridge);
    let uar_split_child = find_group_by_tag(GroupRoleTagV52::UarSplitChildTarget);
    let uar_dept_bundle_locked = find_group_by_tag(GroupRoleTagV52::UarDeptBundleLocked);
    let uar_exc_temp = find_group_by_tag(GroupRoleTagV52::UarExcTemp);
    let uar_exc_valid = find_group_by_tag(GroupRoleTagV52::UarExcValid);
    let uar_priv_audit = find_group_by_tag(GroupRoleTagV52::UarPrivAudit);
    let nested_eng_squad = find_group_by_tag(GroupRoleTagV52::NestedEngSquad);
    let nested_fin_squad = find_group_by_tag(GroupRoleTagV52::NestedFinSquad);
    let nested_adm_sod_left_group = find_group_by_tag(GroupRoleTagV52::NestedAdmSodDeployLeft);

    let all_emp_group = find_group_by_tag(GroupRoleTagV52::AllEmployeeBase);
    let all_emp_entra_group = find_group_by_tag(GroupRoleTagV52::AllEmployeeBaseEntra);
    let fin_group = find_group_by_tag(GroupRoleTagV52::FinanceBirthright);
    let fin_spoke_group = find_group_by_tag(GroupRoleTagV52::FinanceBirthrightSpoke);
    let eng_group = find_group_by_tag(GroupRoleTagV52::EngineeringBirthright);
    let eng_spoke_group = find_group_by_tag(GroupRoleTagV52::EngineeringBirthrightSpoke);
    let it_group = find_group_by_tag(GroupRoleTagV52::ItBirthright);
    let it_spoke_group = find_group_by_tag(GroupRoleTagV52::ItBirthrightSpoke);
    let clin_group = find_group_by_tag(GroupRoleTagV52::ClinicalBirthright);
    let acad_group = find_group_by_tag(GroupRoleTagV52::AcademicBirthright);
    let leg_group = find_group_by_tag(GroupRoleTagV52::LegalBirthright);
    let stu_group = find_group_by_tag(GroupRoleTagV52::StudentBase);
    let cwk_group = find_group_by_tag(GroupRoleTagV52::ContingentBase);
    let nonprod_group = find_group_by_tag(GroupRoleTagV52::NonProdDevNoise);
    let stage_eng_twin = groups
        .iter()
        .find(|g| g.record.group_id.contains("_STAGE_ENG_GIT_USER"))
        .map(|g| g.record.group_id.clone())
        .unwrap_or_default();
    let uat_it_twin = groups
        .iter()
        .find(|g| g.record.group_id.contains("_UAT_IT_DESK_RO"))
        .map(|g| g.record.group_id.clone())
        .unwrap_or_default();

    let find_groups_by_tag = |tag: GroupRoleTagV52| -> Vec<(String, String)> {
        groups
            .iter()
            .filter(|g| g.role_tag == tag)
            .map(|g| (g.record.group_id.clone(), g.record.spoke_id.clone()))
            .collect()
    };
    let decoys_all_emp = find_groups_by_tag(GroupRoleTagV52::NearThresholdDecoyAllEmp);
    let decoys_fin = find_groups_by_tag(GroupRoleTagV52::NearThresholdDecoyFinance);
    let decoys_eng = find_groups_by_tag(GroupRoleTagV52::NearThresholdDecoyEngineering);
    let decoys_it = find_groups_by_tag(GroupRoleTagV52::NearThresholdDecoyIt);
    let decoys_clin = find_groups_by_tag(GroupRoleTagV52::NearThresholdDecoyClinical);
    let decoys_acad = find_groups_by_tag(GroupRoleTagV52::NearThresholdDecoyAcademic);

    RoleGroupIds {
        sod_ap_left_group,
        sod_vendor_right_group,
        sod_deploy_left_group,
        sod_code_right_group,
        priv_admin_group,
        req_clean_a,
        req_clean_b,
        req_clean_c,
        uar_core_vault,
        uar_shared_collab,
        uar_adm_bridge,
        uar_split_child,
        uar_dept_bundle_locked,
        uar_exc_temp,
        uar_exc_valid,
        uar_priv_audit,
        nested_eng_squad,
        nested_fin_squad,
        nested_adm_sod_left_group,
        all_emp_group,
        all_emp_entra_group,
        fin_group,
        fin_spoke_group,
        eng_group,
        eng_spoke_group,
        it_group,
        it_spoke_group,
        clin_group,
        acad_group,
        leg_group,
        stu_group,
        cwk_group,
        nonprod_group,
        stage_eng_twin,
        uat_it_twin,
        decoys_all_emp,
        decoys_fin,
        decoys_eng,
        decoys_it,
        decoys_clin,
        decoys_acad,
    }
}
