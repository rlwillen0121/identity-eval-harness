//! Per-person identity plans: names, departments, lifecycle states, and source membership.

use std::collections::HashSet;

use ib_core::PyRandom;
use indexmap::IndexMap;

use crate::archetypes::{DifficultyTierV52, SecondarySourceKindV52};

use super::WorldCtx;
use super::dates::{prev_day_iso, shift_iso_date};
use super::fill_schedule::{FIXED_PERSONA_COUNT, FillRole};
use super::group_ids::RoleGroupIds;
use super::names::{
    DIACRITIC_NAMES, FIRST_NAMES_ASCII, LAST_NAMES_ASCII, LONG_COLLISION_NAMES, RAW_COUNTRIES,
    compute_base_sam_for_plan, sanitize_sam_token,
};
use super::personas::Personas;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum PairedWithdrawnKind {
    F04SuspendedClinicianWitness,
    F04ActiveClinicianControl,
    F05GardenLeaveWitness,
    F05ActiveExecControl,
}

#[derive(Debug, Clone)]
pub(super) struct PersonPlan {
    pub(super) person_key: String,
    pub(super) badge_id: String,
    pub(super) legal_first: String,
    pub(super) legal_last: String,
    pub(super) pref_first: String,
    pub(super) middle_name: String,
    pub(super) dept_code: String,
    pub(super) dept_canonical: String,
    pub(super) job_code_raw: String,
    pub(super) office_code: String,
    pub(super) cost_center_raw: String,
    pub(super) raw_country: String,
    pub(super) iso_country: String,
    pub(super) manager_key: String,
    pub(super) sponsor_key: String,
    pub(super) workday_status_code: String,
    pub(super) vms_status_code: String,
    pub(super) leave_code: String,
    pub(super) legal_hold_raw: String,
    pub(super) contract_end_date: String,
    pub(super) clinical_license_status: String,
    pub(super) academic_status: String,
    pub(super) workday_aff_code: String,
    pub(super) sec_aff_code: String,
    pub(super) vms_aff_code: String,
    pub(super) in_workday: bool,
    pub(super) in_secondary: bool,
    pub(super) in_vms: bool,
    pub(super) work_email: String,
    pub(super) personal_email: String,
    pub(super) has_ad_account: bool,
    pub(super) ad_enabled: bool,
    pub(super) ad_legal_hold_attr: bool,
    pub(super) ad_use_acquired_domain: bool,
    pub(super) ad_force_sam: Option<String>,
    /// Blank out VMS dept/office while AD already has populated values (null wipeout test!).
    pub(super) blank_vms_dept_and_office: bool,
    /// Blank out Workday dept_code while secondary has `LEGACY-UNMAPPED` and VMS holds valid dept_code
    /// (tests per-source `first_valid([lookup(wd), lookup(sec), lookup(vms)])` fallback!).
    pub(super) blank_workday_dept_fallback_to_vms: bool,
    /// Plant `"SITE-UNMAPPED"` in primary/secondary `office_code` while lower-priority VMS holds valid `office_code`.
    pub(super) office_fallback_site_unmapped: bool,
    /// `0` = normal, `1` = Workday `manager_key="TBD"` (secondary valid), `2` = Workday & secondary `"TBD"` (VMS valid).
    pub(super) manager_tbd_placeholder_sources: u8,
    /// When true on a dual-source `workday + secondary` identity, secondary carries a source-local
    /// `person_key` and non-matching legacy badge `"LEG-8XXX"`, correlating ONLY via `personal_email`.
    pub(super) sec_bridge_personal_email_only: bool,
    /// When true on a dual-source `workday + fieldglass_vms` identity, VMS carries a source-local
    /// `person_key` and non-matching legacy badge `"LEG-9XXX"`, correlating ONLY via `personal_email`.
    pub(super) vms_bridge_personal_email_only: bool,
    /// Mechanism A (`F04`/`F05`): paired `WITHDRAWN`-first + `RECORD_ACTIVE`-second secondary rows.
    pub(super) paired_withdrawn_override: Option<PairedWithdrawnKind>,
    /// Mechanism A (`F11`): one of the 2 active Legal cohort members receiving a `WITHDRAWN` `TERM` Workday duplicate.
    pub(super) f11_legal_cohort_spike_target: bool,
    /// Mechanism `F01-AS`: lagging Workday replica of `academic_status` (Banner SIS stays authoritative).
    pub(super) wd_academic_status_replica: Option<String>,
    /// Mechanism `F02-ST`: raw SAP HCM `STAT2` code rendered in Workday `payroll_code`
    /// (the plan's `workday_status_code` stays the canonical truth).
    pub(super) wd_payroll_code_raw: Option<String>,
    /// Mechanism `F07-WR`: one non-effective Fieldglass work-order revision row
    /// `(record_status, contract_status, contract_end_date)` emitted after the in-force row.
    pub(super) vms_unapproved_revision: Option<(String, String, String)>,
    /// When true, leave the spoke badge/employeeId blank (`""`) across spokes so correlation MUST
    /// fall back to case-insensitive `mail` / `SMTP_ADDR` / `email`.
    pub(super) omit_spoke_badge: bool,
    pub(super) has_sap_account: bool,
    pub(super) has_epic_account: bool,
    pub(super) has_aws_account: bool,
    pub(super) has_custom_rest_account: bool,
    pub(super) extra_ad_groups: Vec<String>,
    pub(super) extra_entra_groups: Vec<String>,
    pub(super) extra_sap_groups: Vec<String>,
    pub(super) extra_aws_groups: Vec<String>,
    pub(super) extra_custom_rest_groups: Vec<String>,
    pub(super) has_adm_secondary: bool,
    pub(super) adm_extra_ad_groups: Vec<String>,
    pub(super) oauth_consents: Vec<String>,
}

/// Person plans plus the handle and name-pool state later phases depend on.
pub(super) struct PersonPlanBuild {
    pub(super) plans_by_key: IndexMap<String, PersonPlan>,
    pub(super) used_sams: HashSet<String>,
    pub(super) first_pool: Vec<&'static str>,
    pub(super) last_pool: Vec<&'static str>,
    pub(super) tomb_base_sam: String,
    pub(super) long_base_sam: String,
    pub(super) loa_4hop_owner: Option<String>,
}

/// Build every person plan in canonical person-key order.
pub(super) fn build_person_plans(
    ctx: &WorldCtx<'_>,
    rng: &mut PyRandom,
    fill_schedule: &[FillRole],
    slot_order: &[usize],
) -> PersonPlanBuild {
    let WorldCtx {
        spec,
        hardening,
        client,
        canonical_person_keys,
        ..
    } = *ctx;
    let Personas {
        ceo_vp,
        active_dir_1,
        active_dir_2,
        garden_leave_mgr,
        dead_root_mgr,
        loa_user,
        glv_mid_mgr,
        terminated_regular,
        ..
    } = ctx.personas.clone();
    let RoleGroupIds {
        sod_ap_left_group,
        sod_deploy_left_group,
        priv_admin_group,
        req_clean_a,
        req_clean_b,
        uar_shared_collab,
        uar_adm_bridge,
        uar_dept_bundle_locked,
        uar_exc_temp,
        uar_exc_valid,
        uar_priv_audit,
        nested_adm_sod_left_group,
        ..
    } = ctx.group_ids.clone();
    // Shuffled human name pools for unique identities.
    let mut first_pool: Vec<&str> = FIRST_NAMES_ASCII.to_vec();
    let mut last_pool: Vec<&str> = LAST_NAMES_ASCII.to_vec();
    rng.shuffle(&mut first_pool);
    rng.shuffle(&mut last_pool);

    let diacritic_choice = DIACRITIC_NAMES[rng.randbelow_usize(DIACRITIC_NAMES.len())];
    let longname_choice = LONG_COLLISION_NAMES[rng.randbelow_usize(LONG_COLLISION_NAMES.len())];

    let dept_entries: Vec<(String, String)> = client
        .dept_lookup
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let office_codes: Vec<String> = client.office_lookup.keys().cloned().collect();
    let cc_codes: Vec<String> = client.cost_center_lookup.keys().cloned().collect();

    let dept_code_for = |canonical: &str| -> (String, String) {
        dept_entries
            .iter()
            .find(|(_, v)| v == canonical)
            .cloned()
            .unwrap_or_else(|| dept_entries[0].clone())
    };

    // Pre-pick exact collision names for tombstone and legal-hold collision tests.
    let lh_first = first_pool[30].to_owned();
    let lh_last = last_pool[30].to_owned();
    let lh_middle = "J".to_owned();
    let lh_base_sam = compute_base_sam_for_plan(
        &spec.base_username_pattern,
        spec.use_preferred_first_name,
        &lh_first,
        "",
        &lh_middle,
        &lh_last,
    );

    let tomb_first = first_pool[31].to_owned();
    let tomb_last = last_pool[31].to_owned();
    let tomb_middle = "K".to_owned();
    let tomb_base_sam = compute_base_sam_for_plan(
        &spec.base_username_pattern,
        spec.use_preferred_first_name,
        &tomb_first,
        "",
        &tomb_middle,
        &tomb_last,
    );

    let long_base_sam = compute_base_sam_for_plan(
        &spec.base_username_pattern,
        spec.use_preferred_first_name,
        longname_choice.0,
        "",
        longname_choice.2,
        longname_choice.1,
    );

    // `F09-DF`: the diacritic new hire's ASCII base handle (its `first_initial_last` base ignores the
    // middle name), held by a disabled acquired-forest account.
    let diacritic_base_sam = compute_base_sam_for_plan(
        &spec.base_username_pattern,
        spec.use_preferred_first_name,
        diacritic_choice.0,
        "",
        "",
        diacritic_choice.1,
    );

    let n_identities = canonical_person_keys.len();

    // Build plans indexed by canonical_person_key order so everything is deterministic.
    let mut plans_by_key: IndexMap<String, PersonPlan> = IndexMap::new();
    let mut used_sams: HashSet<String> = HashSet::new();
    used_sams.insert(lh_base_sam.clone());
    used_sams.insert(tomb_base_sam.clone());
    used_sams.insert(long_base_sam.clone());
    if hardening.disabled_forest_handle_lock {
        used_sams.insert(diacritic_base_sam.clone());
    }

    let badge_base: u32 = u32::try_from(rng.randint(10_000, 79_000)).expect("positive");
    let mut badge_offsets: Vec<usize> = (0..n_identities).collect();
    rng.shuffle(&mut badge_offsets);

    let mut f04_control_assigned = false;
    let mut f05_control_assigned = false;
    let mut f11_legal_spike_assigned = 0usize;
    let mut loa_4hop_owner: Option<String> = None;
    // Appendix C witness assignment (first matching fill slot per mechanism; no RNG consumed).
    let mut f01_alumnus_assigned = false;
    let mut f02_stat2_withdrawn_assigned = false;
    let mut f02_stat2_active_assigned = false;
    let mut f02_stat2_inactive_assigned = false;
    let mut f07_unapproved_revival_assigned = false;

    for (slot_idx, &person_idx) in slot_order.iter().enumerate() {
        let pkey = canonical_person_keys[person_idx].clone();
        let badge_id = format!(
            "{}{:05}",
            client.badge_prefix,
            badge_base + badge_offsets[slot_idx] as u32
        );
        let office_code = office_codes[slot_idx % office_codes.len()].clone();
        let cost_center_raw = cc_codes[slot_idx % cc_codes.len()].clone();
        let (raw_country, iso_country) = RAW_COUNTRIES[slot_idx % RAW_COUNTRIES.len()];

        // Default unique human name from shuffled 29x29 pools (841 distinct pairs, avoiding indices 29..35).
        let name_idx = slot_idx % 29;
        let last_idx = (slot_idx + (slot_idx / 29) * 7) % 29;
        let mut legal_first = first_pool[name_idx].to_owned();
        let mut legal_last = last_pool[last_idx].to_owned();
        let mut pref_first = String::new();
        let mut middle_name =
            ["A", "B", "C", "D", "E", "G", "H", "L", "M", "R"][slot_idx % 10].to_owned();

        let (mut dept_code, mut dept_canonical) = match slot_idx % 5 {
            0 => dept_code_for("Engineering"),
            1 => dept_code_for("Finance"),
            2 => dept_code_for("Information Technology"),
            3 => dept_code_for("Legal"),
            _ => dept_code_for("Human Resources"),
        };
        let job_code_for_dept = |d: &str| -> String {
            match d {
                "Finance" => "fin_analyst".to_owned(),
                "Engineering" => "eng_sr".to_owned(),
                "Information Technology" => "it_ops".to_owned(),
                "Legal" => "leg_counsel".to_owned(),
                "Clinical Operations" => "clin_md".to_owned(),
                "Academic Affairs" => "acad_fac".to_owned(),
                _ => "hr_partner".to_owned(),
            }
        };
        let mut job_code_raw = job_code_for_dept(&dept_canonical);

        let mut manager_key = if slot_idx % 2 == 0 {
            active_dir_1.clone()
        } else {
            active_dir_2.clone()
        };
        let mut sponsor_key = String::new();
        let mut workday_status_code = "ACT".to_owned();
        let mut vms_status_code = String::new();
        let mut leave_code = String::new();
        let mut legal_hold_raw = "N".to_owned();
        let mut contract_end_date = String::new();
        let mut clinical_license_status = String::new();
        let mut academic_status = String::new();
        let mut workday_aff_code = "FTE".to_owned();
        let mut sec_aff_code = String::new();
        let mut vms_aff_code = String::new();
        let mut in_workday = true;
        let mut in_secondary = false;
        let mut in_vms = false;
        let mut has_ad_account = true;
        let mut ad_enabled = true;
        let mut ad_legal_hold_attr = false;
        let mut ad_use_acquired_domain = false;
        let mut ad_force_sam: Option<String> = None;
        let mut blank_vms_dept_and_office = false;
        let mut blank_workday_dept_fallback_to_vms = false;
        let mut office_fallback_site_unmapped = false;
        let mut manager_tbd_placeholder_sources: u8 = 0;
        let mut sec_bridge_personal_email_only = false;
        let mut vms_bridge_personal_email_only = false;
        let mut paired_withdrawn_override: Option<PairedWithdrawnKind> = None;
        let mut f11_legal_cohort_spike_target = false;
        let mut wd_academic_status_replica: Option<String> = None;
        let mut wd_payroll_code_raw: Option<String> = None;
        let mut vms_unapproved_revision: Option<(String, String, String)> = None;
        let mut has_sap_account = false;
        let mut has_epic_account = false;
        let mut has_aws_account = false;
        let mut has_custom_rest_account = false;
        let mut extra_ad_groups: Vec<String> = Vec::new();
        let extra_entra_groups: Vec<String> = Vec::new();
        let mut extra_sap_groups: Vec<String> = Vec::new();
        let mut extra_aws_groups: Vec<String> = Vec::new();
        let mut extra_custom_rest_groups: Vec<String> = Vec::new();
        let mut has_adm_secondary = false;
        let mut adm_extra_ad_groups: Vec<String> = Vec::new();

        match slot_idx {
            0 => {
                // ceo_vp: Active Finance FTE (final custodian of 3-hop emergency termination!)
                manager_key.clear();
                job_code_raw = "exec_vp".to_owned();
                (dept_code, dept_canonical) = dept_code_for("Finance");
                has_sap_account = spec.has_sap;
                has_custom_rest_account = true;
                extra_custom_rest_groups.push(req_clean_b.clone());
            }
            1 => {
                // active_dir_1: Active Engineering FTE (2nd-level active director custodian, adm_* #1, UAR colleague z!)
                manager_key = ceo_vp.clone();
                job_code_raw = "dir_eng".to_owned();
                (dept_code, dept_canonical) = dept_code_for("Engineering");
                has_aws_account = spec.has_aws;
                has_custom_rest_account = true;
                extra_custom_rest_groups.push(req_clean_a.clone());
                extra_ad_groups.push(uar_dept_bundle_locked.clone());
                has_adm_secondary = true;
                adm_extra_ad_groups.push(priv_admin_group.clone());
            }
            2 => {
                // active_dir_2: Active IT FTE (adm_* #2!)
                manager_key = ceo_vp.clone();
                job_code_raw = "dir_ops".to_owned();
                (dept_code, dept_canonical) = dept_code_for("Information Technology");
                has_aws_account = spec.has_aws;
                has_custom_rest_account = true;
                has_adm_secondary = true;
                adm_extra_ad_groups.push(priv_admin_group.clone());
            }
            3 => {
                // garden_leave_mgr: HR payroll ACTIVE, leave_type GARDEN_LEAVE, manager = active_dir_1!
                manager_key = active_dir_1.clone();
                (dept_code, dept_canonical) = dept_code_for("Finance");
                workday_status_code = "ACT".to_owned();
                leave_code = "GLV".to_owned();
                job_code_raw = "vp_strat".to_owned();
                has_sap_account = spec.has_sap;
                has_custom_rest_account = true;
                if spec.family_id == "F05" {
                    in_secondary = true;
                    sec_bridge_personal_email_only = true;
                    paired_withdrawn_override = Some(PairedWithdrawnKind::F05GardenLeaveWitness);
                }
            }
            4 => {
                // emerg_term_2hop: Active Engineering FTE reporting to garden_leave_mgr!
                manager_key = garden_leave_mgr.clone();
                (dept_code, dept_canonical) = dept_code_for("Engineering");
                job_code_raw = "eng_sr".to_owned();
                has_sap_account = spec.has_sap;
                has_aws_account = spec.has_aws;
                has_custom_rest_account = true;
                extra_custom_rest_groups.push(req_clean_a.clone());
            }
            5 => {
                // dead_root_mgr: Terminated SVP with no manager above
                manager_key.clear();
                (dept_code, dept_canonical) = dept_code_for("Legal");
                workday_status_code = "TERM".to_owned();
                ad_enabled = false;
                if hardening.disabled_forest_handle_lock {
                    // `F09-DF`: the disabled acquired-forest account keeps its legacy-domain UPN (so ISC
                    // never preserves it) and holds the diacritic new hire's ASCII base handle.
                    ad_use_acquired_domain = true;
                    ad_force_sam = Some(diacritic_base_sam.clone());
                }
            }
            6 => {
                // emerg_term_fallback: Active Legal FTE reporting to a 3-inactive manager chain
                // terminated_regular (Term, hop 1) -> glv_mid_mgr (GLV, hop 2) -> loa_user (LOA, hop 3)
                // -> ceo_vp (Active VP on hop 4, exceeding MAX_LIVE_MANAGER_CHAIN_HOPS = 3 -> fallback)!
                manager_key = terminated_regular.clone();
                (dept_code, dept_canonical) = dept_code_for("Legal");
                job_code_raw = "leg_counsel".to_owned();
            }
            7 => {
                // day2_glv_1: Active Finance executive placed on Garden Leave on Day 2
                manager_key = ceo_vp.clone();
                (dept_code, dept_canonical) = dept_code_for("Finance");
                job_code_raw = "fin_analyst".to_owned();
                has_sap_account = spec.has_sap;
                has_custom_rest_account = true;
                extra_custom_rest_groups.push(req_clean_b.clone());
            }
            8 => {
                // eng_sod_requester: Active Engineering FTE placed on page 2 of spoke_accounts.json!
                // Already holds SodDeployApproveLeft in AWS (or AD), requests SodCodeCommitRight on Day 2!
                manager_key = active_dir_1.clone();
                (dept_code, dept_canonical) = dept_code_for("Engineering");
                job_code_raw = "eng_sr".to_owned();
                has_aws_account = spec.has_aws;
                if spec.has_aws {
                    extra_aws_groups.push(sod_deploy_left_group.clone());
                } else {
                    extra_ad_groups.push(sod_deploy_left_group.clone());
                }
            }
            9 => {
                // legal_hold_user: Terminated + Legal Hold, disabled AD account holding lh_base_sam!
                manager_key = active_dir_1.clone();
                (dept_code, dept_canonical) = dept_code_for("Legal");
                workday_status_code = "TERM".to_owned();
                leave_code = "LHA".to_owned();
                legal_hold_raw = "YES".to_owned();
                ad_enabled = false;
                ad_legal_hold_attr = true;
                ad_force_sam = Some(lh_base_sam.clone());
                has_sap_account = spec.has_sap;
            }
            10 => {
                // furlough_user: 9-month Faculty or Seasonal Worker on Furlough (denied headless intake on Day 2!)
                manager_key = active_dir_2.clone();
                workday_status_code = "ACT".to_owned();
                leave_code = "FUR".to_owned();
                academic_status = "SUMMER_FURLOUGH".to_owned();
                if spec.secondary_source == SecondarySourceKindV52::BannerSis {
                    workday_aff_code = "FAC".to_owned();
                    (dept_code, dept_canonical) = dept_code_for("Academic Affairs");
                    job_code_raw = "acad_fac".to_owned();
                } else {
                    workday_aff_code = "SEAS".to_owned();
                    (dept_code, dept_canonical) = dept_code_for("Information Technology");
                    job_code_raw = "it_ops".to_owned();
                }
                has_sap_account = spec.has_sap;
                has_epic_account = spec.has_epic;
            }
            11 => {
                // student_worker_ended: Workday student-worker job TERMINATED, still ENROLLED student!
                manager_key = active_dir_2.clone();
                workday_status_code = "TERM".to_owned();
                workday_aff_code = "STW".to_owned();
                academic_status = "ENROLLED".to_owned();
                if spec.secondary_source == SecondarySourceKindV52::BannerSis {
                    in_secondary = true;
                    sec_aff_code = "STU".to_owned();
                }
                if hardening.banner_academic_standing_sor {
                    // `F01-AS` AB witness: Workday's replica lags fall registration; Banner says ENROLLED.
                    wd_academic_status_replica = Some("NOT_ENROLLED".to_owned());
                }
                (dept_code, dept_canonical) = dept_code_for("Academic Affairs");
                job_code_raw = "stu_asst".to_owned();
            }
            12 => {
                // contingent_day1_grace: Contractor whose contract_end_date is 5 days before cutover_date
                // (strictly inside 7/10/14-day grace window across all regimes!)
                in_workday = false;
                in_vms = true;
                (dept_code, dept_canonical) = dept_code_for("Engineering");
                vms_status_code = "ACT".to_owned();
                vms_aff_code = "CWK".to_owned();
                contract_end_date = prev_day_iso(&client.cutover_date, 5);
                sponsor_key = active_dir_1.clone();
                manager_key = active_dir_1.clone();
                blank_vms_dept_and_office = true;
                has_custom_rest_account = true;
                if hardening.vms_revision_approval_ledger {
                    // `F07-WR`: a REJECTED extension revision must not lift the grace suspension.
                    vms_unapproved_revision = Some((
                        "REJECTED".to_owned(),
                        "ACT".to_owned(),
                        shift_iso_date(&client.cutover_date, 90),
                    ));
                }
            }
            13 => {
                // contingent_day2_active_sp: Active contractor expiring on Day 2 with active sponsor
                in_workday = false;
                in_vms = true;
                (dept_code, dept_canonical) = dept_code_for("Finance");
                vms_status_code = "ACT".to_owned();
                vms_aff_code = "CWK".to_owned();
                contract_end_date = "2026-12-20".to_owned();
                sponsor_key = active_dir_2.clone();
                manager_key = active_dir_1.clone();
                blank_vms_dept_and_office = true;
            }
            14 => {
                // contingent_day2_dead_sp_glv_mgr: Active contractor expiring on Day 2 with TERMINATED sponsor
                // and GARDEN_LEAVE manager -> walks past GLV manager to 2nd-level Active director (`active_dir_1`)!
                in_workday = false;
                in_vms = true;
                (dept_code, dept_canonical) = dept_code_for("Engineering");
                vms_status_code = "ACT".to_owned();
                vms_aff_code = "CWK".to_owned();
                contract_end_date = "2026-12-28".to_owned();
                sponsor_key = dead_root_mgr.clone();
                manager_key = garden_leave_mgr.clone();
                blank_vms_dept_and_office = true;
            }
            15 => {
                // contingent_day2_exhausted: Active contractor expiring on Day 2 whose sponsor AND manager
                // are both Terminated with no active ancestor -> falls back to `fallback_vms_pmo_queue`!
                in_workday = false;
                in_vms = true;
                (dept_code, dept_canonical) = dept_code_for("Information Technology");
                vms_status_code = "ACT".to_owned();
                vms_aff_code = "CWK".to_owned();
                contract_end_date = "2026-12-29".to_owned();
                sponsor_key = dead_root_mgr.clone();
                manager_key = dead_root_mgr.clone();
                blank_vms_dept_and_office = true;
            }
            16 => {
                // suspended_clinician: Clinical provider with SUSPENDED license (payroll still ACTIVE!)
                manager_key = active_dir_2.clone();
                workday_status_code = "ACT".to_owned();
                workday_aff_code = "CLIN".to_owned();
                clinical_license_status = "SUSPENDED".to_owned();
                if spec.secondary_source == SecondarySourceKindV52::Credentialing {
                    in_secondary = true;
                    sec_aff_code = "CLIN".to_owned();
                }
                if spec.family_id == "F04" {
                    in_secondary = true;
                    sec_aff_code = "CLIN".to_owned();
                    sec_bridge_personal_email_only = true;
                    paired_withdrawn_override =
                        Some(PairedWithdrawnKind::F04SuspendedClinicianWitness);
                }
                (dept_code, dept_canonical) = dept_code_for("Clinical Operations");
                job_code_raw = "clin_md".to_owned();
                has_epic_account = spec.has_epic;
                has_sap_account = spec.has_sap;
            }
            17 => {
                // loa_user: Parental LOA (Hop 3 of 3-hop emergency termination chain -> ceo_vp)
                manager_key = ceo_vp.clone();
                (dept_code, dept_canonical) = dept_code_for("Engineering");
                workday_status_code = "LOA".to_owned();
                leave_code = "PAR".to_owned();
            }
            18 => {
                // glv_mid_mgr: GardenLeave mid-manager (Hop 2 of 3-hop emergency termination chain -> loa_user)
                manager_key = loa_user.clone();
                (dept_code, dept_canonical) = dept_code_for("Finance");
                workday_status_code = "ACT".to_owned();
                leave_code = "GLV".to_owned();
            }
            19 => {
                // terminated_regular: Terminated manager (Hop 1 of 3-hop emergency termination chain -> glv_mid_mgr)
                manager_key = glv_mid_mgr.clone();
                (dept_code, dept_canonical) = dept_code_for("Finance");
                workday_status_code = "TERM".to_owned();
                ad_enabled = false;
            }
            20 => {
                // emerg_term_3hop: Active Finance FTE holding 20-char long_base_sam AND reaching
                // ceo_vp (Active VP) on exact hop 3: glv_mid_mgr (GLV, hop 1) -> loa_user (LOA, hop 2) -> ceo_vp (hop 3)!
                manager_key = glv_mid_mgr.clone();
                (dept_code, dept_canonical) = dept_code_for("Finance");
                job_code_raw = "fin_analyst".to_owned();
                ad_force_sam = Some(long_base_sam.clone());
                has_sap_account = spec.has_sap;
            }
            21 => {
                // prehire_user: Pre-Hire (no AD account yet)
                workday_status_code = "PRE".to_owned();
                has_ad_account = false;
            }
            22 => {
                // diacritic_newhire: Uncorrelated new hire with diacritics & hyphen/apostrophe!
                legal_first = diacritic_choice.0.to_owned();
                legal_last = diacritic_choice.1.to_owned();
                pref_first.clear();
                has_ad_account = false;
            }
            23 => {
                // longname_collision_newhire: Uncorrelated new hire with >20-char base colliding with live AD + tombstones!
                legal_first = longname_choice.0.to_owned();
                legal_last = longname_choice.1.to_owned();
                middle_name = longname_choice.2.to_owned();
                pref_first.clear();
                has_ad_account = false;
            }
            24 => {
                // tombstone_collision_newhire: Uncorrelated new hire colliding with 7-year tombstone archive!
                legal_first = tomb_first.clone();
                legal_last = tomb_last.clone();
                middle_name = tomb_middle.clone();
                pref_first.clear();
                has_ad_account = false;
            }
            25 => {
                // legalhold_collision_newhire: Uncorrelated new hire colliding with LegalHold disabled account!
                legal_first = lh_first.clone();
                legal_last = lh_last.clone();
                middle_name = lh_middle.clone();
                pref_first.clear();
                has_ad_account = false;
            }
            26 => {
                // preferred_name_newhire: Uncorrelated new hire with preferred_first_name != legal_first_name!
                legal_first = "William".to_owned();
                pref_first = "Liam".to_owned();
                legal_last = last_pool[32].to_owned();
                middle_name = "T".to_owned();
                has_ad_account = false;
            }
            27 => {
                // ma_acquired_user: Secondary-only FTE whose brownfield AD mail uses acquired domain
                // while HR work_email uses primary domain (proves immutable employeeId MUST be Rule 1!).
                in_workday = false;
                in_secondary = true;
                sec_aff_code = "FTE".to_owned();
                workday_aff_code.clear();
                ad_use_acquired_domain = true;
                (dept_code, dept_canonical) = dept_code_for("Engineering");
                job_code_raw = "eng_sr".to_owned();
                has_aws_account = spec.has_aws;
                has_custom_rest_account = !spec.has_aws;
            }
            28 => {
                // finance_sod_requester: Active Finance FTE on page 2 of spoke_accounts.json!
                // Already holds SodApPostLeft in SAP (or AD), requests SodVendorMaintRight on Day 2!
                (dept_code, dept_canonical) = dept_code_for("Finance");
                job_code_raw = "fin_ap".to_owned();
                has_sap_account = spec.has_sap;
                if spec.has_sap {
                    extra_sap_groups.push(sod_ap_left_group.clone());
                } else {
                    extra_ad_groups.push(sod_ap_left_group.clone());
                }
            }
            29 => {
                // contractor_priv_requester: Active Contingent Worker requesting Privileged Admin on Day 2!
                in_workday = false;
                in_vms = true;
                vms_status_code = "ACT".to_owned();
                vms_aff_code = "CWK".to_owned();
                contract_end_date = "2027-03-31".to_owned();
                sponsor_key = active_dir_1.clone();
                (dept_code, dept_canonical) = dept_code_for("Information Technology");
                blank_vms_dept_and_office = true;
            }
            30 => {
                // happy_path_requester: Active Engineering FTE requesting clean app A (does NOT hold req_clean_a!)
                (dept_code, dept_canonical) = dept_code_for("Engineering");
                job_code_raw = "eng_sr".to_owned();
                has_aws_account = spec.has_aws;
                has_custom_rest_account = true;
            }
            31 => {
                // phantom_close_requester: Active IT FTE requesting clean app B (ticket closed without aggregation)
                // Also adm_* #3 (UAR COI self-review subject holding UarPrivAudit on adm_*)
                (dept_code, dept_canonical) = dept_code_for("Information Technology");
                job_code_raw = "it_ops".to_owned();
                has_aws_account = spec.has_aws;
                has_custom_rest_account = true;
                has_adm_secondary = true;
                adm_extra_ad_groups.push(uar_priv_audit.clone());
            }
            32 => {
                // unapproved_requester: Active Legal FTE requesting clean app C without app owner approval
                // Also holds valid compensating exception entitlement for UAR
                (dept_code, dept_canonical) = dept_code_for("Legal");
                job_code_raw = "leg_counsel".to_owned();
                extra_ad_groups.push(uar_exc_valid.clone());
            }
            33 => {
                // Compound Dual-State #1: GARDEN_LEAVE + LEGAL_HOLD -> must resolve to LegalHold (#1 over #2)!
                manager_key = active_dir_1.clone();
                (dept_code, dept_canonical) = dept_code_for("Legal");
                workday_status_code = "ACT".to_owned();
                leave_code = "GLV".to_owned();
                legal_hold_raw = "YES".to_owned();
                ad_enabled = false;
                ad_legal_hold_attr = true;
                has_sap_account = spec.has_sap;
            }
            34 => {
                // Compound Dual-State #2: FURLOUGH + SUSPENDED clinical license -> SuspendedCredential (#3 over #6)!
                manager_key = active_dir_2.clone();
                workday_status_code = "ACT".to_owned();
                workday_aff_code = "CLIN".to_owned();
                leave_code = "FUR".to_owned();
                clinical_license_status = "SUSPENDED".to_owned();
                if spec.secondary_source == SecondarySourceKindV52::Credentialing {
                    in_secondary = true;
                    sec_aff_code = "CLIN".to_owned();
                }
                (dept_code, dept_canonical) = dept_code_for("Clinical Operations");
                job_code_raw = "clin_md".to_owned();
                has_epic_account = spec.has_epic;
            }
            35 => {
                // fallback_user_1: Active Engineering FTE; on FullStack + F02 + F09 tests per-source dept/office fallback + TBD manager!
                // Also holds locked parent bundle for UAR bundle split
                (dept_code, dept_canonical) = dept_code_for("Engineering");
                job_code_raw = "eng_sr".to_owned();
                has_aws_account = spec.has_aws;
                has_custom_rest_account = !spec.has_aws;
                extra_ad_groups.push(uar_dept_bundle_locked.clone());
                if spec.tier == DifficultyTierV52::FullStack
                    || matches!(spec.family_id, "F02" | "F09")
                {
                    in_workday = true;
                    in_secondary = true;
                    in_vms = true;
                    if spec.family_id == "F09" {
                        vms_bridge_personal_email_only = true;
                    }
                    workday_status_code = "ACT".to_owned();
                    vms_status_code = "ACT".to_owned();
                    workday_aff_code = "FTE".to_owned();
                    sec_aff_code.clear();
                    vms_aff_code.clear();
                    contract_end_date = "2027-06-30".to_owned();
                    sponsor_key = active_dir_1.clone();
                    blank_vms_dept_and_office = false;
                    blank_workday_dept_fallback_to_vms = true;
                    if spec.tier == DifficultyTierV52::FullStack || spec.family_id == "F09" {
                        office_fallback_site_unmapped = true;
                        manager_tbd_placeholder_sources = 1;
                    }
                }
            }
            36 => {
                // fallback_user_2: Active Finance FTE; adm_* #6, UAR colleague y holding shared collab group!
                (dept_code, dept_canonical) = dept_code_for("Finance");
                job_code_raw = if hardening.uar_readback_closure {
                    "fin_treasury".to_owned()
                } else {
                    "fin_analyst".to_owned()
                };
                has_sap_account = spec.has_sap;
                has_adm_secondary = true;
                extra_ad_groups.push(uar_shared_collab.clone());
                adm_extra_ad_groups.push(priv_admin_group.clone());
                if spec.tier == DifficultyTierV52::FullStack
                    || matches!(spec.family_id, "F02" | "F09")
                {
                    in_workday = true;
                    in_secondary = true;
                    in_vms = true;
                    workday_status_code = "ACT".to_owned();
                    vms_status_code = "ACT".to_owned();
                    workday_aff_code = "FTE".to_owned();
                    sec_aff_code.clear();
                    vms_aff_code.clear();
                    contract_end_date = "2027-06-30".to_owned();
                    sponsor_key = active_dir_2.clone();
                    blank_vms_dept_and_office = false;
                    blank_workday_dept_fallback_to_vms = true;
                    if spec.tier == DifficultyTierV52::FullStack || spec.family_id == "F09" {
                        office_fallback_site_unmapped = true;
                        manager_tbd_placeholder_sources = 2;
                    }
                }
            }
            37 => {
                // Multi-affiliation / secondary-source overlap showcase
                in_workday = true;
                in_secondary = true;
                match spec.secondary_source {
                    SecondarySourceKindV52::BannerSis => {
                        workday_aff_code = "STW".to_owned();
                        sec_aff_code = "STU".to_owned();
                        academic_status = "ENROLLED".to_owned();
                        (dept_code, dept_canonical) = dept_code_for("Academic Affairs");
                        job_code_raw = "stu_asst".to_owned();
                    }
                    SecondarySourceKindV52::Credentialing => {
                        workday_aff_code = "FAC".to_owned();
                        sec_aff_code = "CLIN".to_owned();
                        clinical_license_status = "ACTIVE".to_owned();
                        (dept_code, dept_canonical) = dept_code_for("Clinical Operations");
                        job_code_raw = "clin_md".to_owned();
                        has_epic_account = spec.has_epic;
                    }
                    SecondarySourceKindV52::AcquiredHr => {
                        sec_aff_code = "FTE".to_owned();
                        (dept_code, dept_canonical) = dept_code_for("Human Resources");
                        job_code_raw = "hr_partner".to_owned();
                    }
                }
            }
            38 => {
                // b2b_guest_1: Active B2B Guest partner whose sponsor is departed (walks manager chain on Day 2!)
                in_workday = false;
                in_vms = true;
                (dept_code, dept_canonical) = dept_code_for("Engineering");
                job_code_raw = "b2b_partner".to_owned();
                vms_status_code = "ACT".to_owned();
                vms_aff_code = "B2B_GUEST".to_owned();
                contract_end_date = "2027-04-30".to_owned();
                sponsor_key = dead_root_mgr.clone();
                manager_key = garden_leave_mgr.clone();
                blank_vms_dept_and_office = true;
            }
            39 => {
                // b2b_guest_2: Active B2B Guest partner with active sponsor
                in_workday = false;
                in_vms = true;
                (dept_code, dept_canonical) = dept_code_for("Finance");
                job_code_raw = "b2b_partner".to_owned();
                vms_status_code = "ACT".to_owned();
                vms_aff_code = "B2B_GUEST".to_owned();
                contract_end_date = "2027-06-30".to_owned();
                sponsor_key = active_dir_2.clone();
                manager_key = active_dir_2.clone();
                blank_vms_dept_and_office = true;
            }
            40 => {
                // adm_eng_sod_user: Active Engineering FTE with adm_* #4 holding SodDeployApproveLeft
                // (via nested AD group SG_{pfx}_PRD_REL_GATE_ADM on F10/F12)!
                manager_key = active_dir_1.clone();
                (dept_code, dept_canonical) = dept_code_for("Engineering");
                job_code_raw = "eng_sr".to_owned();
                has_aws_account = spec.has_aws;
                has_adm_secondary = true;
                if matches!(spec.family_id, "F10" | "F12") && !nested_adm_sod_left_group.is_empty()
                {
                    adm_extra_ad_groups.push(nested_adm_sod_left_group.clone());
                } else {
                    adm_extra_ad_groups.push(sod_deploy_left_group.clone());
                }
            }
            41 => {
                // adm_fin_uar_dual: Active Finance FTE with adm_* #5 (UAR dual-path subject & OBO HITL requester!)
                manager_key = ceo_vp.clone();
                (dept_code, dept_canonical) = dept_code_for("Finance");
                job_code_raw = "fin_analyst".to_owned();
                has_sap_account = spec.has_sap;
                has_adm_secondary = true;
                extra_ad_groups.push(uar_shared_collab.clone());
                adm_extra_ad_groups.push(uar_adm_bridge.clone());
            }
            42 => {
                // uar_exp_exc_user: Active IT FTE reporting to garden_leave_mgr, holding expired UAR exception group!
                manager_key = garden_leave_mgr.clone();
                (dept_code, dept_canonical) = dept_code_for("Information Technology");
                job_code_raw = "it_ops".to_owned();
                extra_ad_groups.push(uar_exc_temp.clone());
            }
            other => {
                let fill = fill_schedule[other - FIXED_PERSONA_COUNT];
                match fill {
                    FillRole::ActiveFte(d) => {
                        (dept_code, dept_canonical) = dept_code_for(d);
                        job_code_raw = job_code_for_dept(d);
                        workday_aff_code = "FTE".to_owned();
                        if d == "Finance" {
                            has_sap_account = spec.has_sap;
                            has_custom_rest_account = spec.tier == DifficultyTierV52::Focused;
                            if hardening.hcm_stat2_payroll_codes && !f02_stat2_active_assigned {
                                // `F02-ST` control: STAT2 `3` (active) = ACTIVE.
                                wd_payroll_code_raw = Some("3".to_owned());
                                f02_stat2_active_assigned = true;
                            }
                            if spec.family_id == "F05" && !f05_control_assigned {
                                in_secondary = true;
                                sec_bridge_personal_email_only = true;
                                paired_withdrawn_override =
                                    Some(PairedWithdrawnKind::F05ActiveExecControl);
                                f05_control_assigned = true;
                            }
                        } else if d == "Engineering" {
                            has_aws_account = spec.has_aws;
                            has_custom_rest_account = spec.tier == DifficultyTierV52::Focused
                                || !spec.has_aws
                                || other % 3 == 0;
                        } else if d == "Information Technology" {
                            has_aws_account = spec.has_aws && other % 2 == 0;
                            has_custom_rest_account = true;
                        } else if d == "Legal" {
                            if spec.tier == DifficultyTierV52::Focused {
                                has_custom_rest_account = true;
                            }
                            if spec.family_id == "F11" && f11_legal_spike_assigned < 2 {
                                f11_legal_cohort_spike_target = true;
                                f11_legal_spike_assigned += 1;
                            }
                        }
                    }
                    FillRole::ActiveClinical(is_provider) => {
                        (dept_code, dept_canonical) = dept_code_for("Clinical Operations");
                        job_code_raw = "clin_md".to_owned();
                        workday_aff_code = if is_provider { "CLIN" } else { "RES" }.to_owned();
                        clinical_license_status = "ACTIVE".to_owned();
                        if spec.secondary_source == SecondarySourceKindV52::Credentialing {
                            in_secondary = true;
                            sec_aff_code = workday_aff_code.clone();
                        }
                        if spec.family_id == "F04" && is_provider && !f04_control_assigned {
                            in_secondary = true;
                            sec_aff_code = workday_aff_code.clone();
                            sec_bridge_personal_email_only = true;
                            paired_withdrawn_override =
                                Some(PairedWithdrawnKind::F04ActiveClinicianControl);
                            f04_control_assigned = true;
                        }
                        has_epic_account = spec.has_epic;
                        has_custom_rest_account = spec.tier == DifficultyTierV52::Focused;
                    }
                    FillRole::ActiveAcademic(is_ft) => {
                        (dept_code, dept_canonical) = dept_code_for("Academic Affairs");
                        job_code_raw = "acad_fac".to_owned();
                        workday_aff_code = if is_ft { "FAC" } else { "ADJ" }.to_owned();
                        has_custom_rest_account = spec.tier == DifficultyTierV52::Focused;
                    }
                    FillRole::ActiveStudent(d) => {
                        (dept_code, dept_canonical) = dept_code_for(d);
                        job_code_raw = "stu_asst".to_owned();
                        workday_aff_code.clear();
                        academic_status = "ENROLLED".to_owned();
                        if spec.secondary_source == SecondarySourceKindV52::BannerSis {
                            in_workday = false;
                            in_secondary = true;
                            sec_aff_code = "STU".to_owned();
                        }
                    }
                    FillRole::ActiveVms(d, is_cwk) => {
                        (dept_code, dept_canonical) = dept_code_for(d);
                        job_code_raw = "cwk_eng".to_owned();
                        in_workday = false;
                        in_vms = true;
                        vms_status_code = "ACT".to_owned();
                        vms_aff_code = if is_cwk { "CWK" } else { "VND" }.to_owned();
                        contract_end_date = "2027-05-31".to_owned();
                        sponsor_key = if other % 2 == 0 {
                            active_dir_1.clone()
                        } else {
                            active_dir_2.clone()
                        };
                        blank_vms_dept_and_office = other % 3 == 0;
                        has_custom_rest_account =
                            spec.tier == DifficultyTierV52::Focused || other % 2 == 0;
                        if hardening.vms_revision_approval_ledger
                            && is_cwk
                            && !f07_unapproved_revival_assigned
                        {
                            // `F07-WR` OG witness: the in-force work order was ended early (`TERM`);
                            // the sponsor's re-extension request is still PENDING_APPROVAL.
                            vms_status_code = "TERM".to_owned();
                            contract_end_date = shift_iso_date(&client.cutover_date, -45);
                            ad_enabled = false;
                            vms_unapproved_revision = Some((
                                "PENDING_APPROVAL".to_owned(),
                                "ACT".to_owned(),
                                shift_iso_date(&client.cutover_date, 180),
                            ));
                            f07_unapproved_revival_assigned = true;
                        }
                    }
                    FillRole::NonActiveTerm(d) => {
                        (dept_code, dept_canonical) = dept_code_for(d);
                        job_code_raw = job_code_for_dept(d);
                        workday_status_code = "TERM".to_owned();
                        ad_enabled = false;
                        if d == "Clinical Operations" {
                            workday_aff_code = "CLIN".to_owned();
                            has_epic_account = spec.has_epic;
                        } else if d == "Academic Affairs" {
                            workday_aff_code = "FAC".to_owned();
                        } else if d == "Finance" {
                            has_sap_account = spec.has_sap;
                        }
                        if hardening.banner_academic_standing_sor
                            && d == "Academic Affairs"
                            && !f01_alumnus_assigned
                        {
                            // `F01-AS` OG witness: a former student worker who graduated (Banner) while
                            // Workday's replica still reads ENROLLED.
                            workday_aff_code = "STW".to_owned();
                            job_code_raw = "stu_asst".to_owned();
                            academic_status = "GRADUATED".to_owned();
                            in_secondary = true;
                            sec_aff_code.clear();
                            wd_academic_status_replica = Some("ENROLLED".to_owned());
                            f01_alumnus_assigned = true;
                        }
                        if hardening.hcm_stat2_payroll_codes
                            && d == "Finance"
                            && !f02_stat2_withdrawn_assigned
                        {
                            // `F02-ST` OG witness: STAT2 `0` (withdrawn) = TERMINATED.
                            wd_payroll_code_raw = Some("0".to_owned());
                            f02_stat2_withdrawn_assigned = true;
                        }
                    }
                    FillRole::NonActiveLoa(d) => {
                        (dept_code, dept_canonical) = dept_code_for(d);
                        job_code_raw = job_code_for_dept(d);
                        workday_status_code = "LOA".to_owned();
                        leave_code = "PAR".to_owned();
                        ad_enabled = false;
                        if hardening.hcm_stat2_payroll_codes
                            && d == "Finance"
                            && !f02_stat2_inactive_assigned
                        {
                            // `F02-ST` witness: STAT2 `1` (inactive) = LOA, never ACTIVE.
                            wd_payroll_code_raw = Some("1".to_owned());
                            f02_stat2_inactive_assigned = true;
                        }
                        if d == "Clinical Operations" {
                            workday_aff_code = "CLIN".to_owned();
                            has_epic_account = spec.has_epic;
                        } else if d == "Academic Affairs" {
                            workday_aff_code = "FAC".to_owned();
                        } else if d == "Finance" {
                            has_sap_account = spec.has_sap;
                        } else if d == "Information Technology"
                            && matches!(spec.family_id, "F05" | "F12")
                            && loa_4hop_owner.is_none()
                        {
                            // Hop 1 = terminated_regular -> Hop 2 = glv_mid_mgr -> Hop 3 = loa_user -> Hop 4 = ceo_vp (exceeds 3-hop cap!)
                            manager_key = terminated_regular.clone();
                            loa_4hop_owner = Some(pkey.clone());
                        }
                    }
                }
            }
        }

        // Select ~16.7% of correlated brownfield accounts to omit spoke badges (forcing email fallback),
        // excluding `ma_acquired_user` (slot 27) and `adm_*` holders so `adm_*` correlates via `employeeId`.
        let omit_spoke_badge = !has_adm_secondary
            && has_ad_account
            && !ad_use_acquired_domain
            && (person_idx % 6 == 2);
        let work_email_use_acquired_domain =
            omit_spoke_badge && in_workday && (person_idx % 24 == 2);

        let oauth_consents = if workday_status_code == "TERM"
            || leave_code == "GLV"
            || legal_hold_raw == "YES"
            || clinical_license_status == "SUSPENDED"
            || clinical_license_status == "EXPIRED"
        {
            vec![
                "M365_Graph_Offline".to_owned(),
                "Salesforce_ConnectedApp".to_owned(),
            ]
        } else {
            Vec::new()
        };

        let email_domain = if work_email_use_acquired_domain {
            &client.acquired_domain
        } else if in_vms && !in_workday && vms_aff_code == "B2B_GUEST" {
            &client.vendor_domain
        } else if in_vms && !in_workday {
            &client.contractor_domain
        } else if academic_status == "ENROLLED"
            && (workday_aff_code.is_empty()
                || (workday_aff_code == "STW" && workday_status_code == "TERM"))
        {
            &client.student_domain
        } else {
            &client.primary_domain
        };
        let email_local = format!(
            "{}.{}{}",
            sanitize_sam_token(&legal_first)
                .chars()
                .take(1)
                .collect::<String>(),
            sanitize_sam_token(&legal_last)
                .chars()
                .take(3)
                .collect::<String>(),
            person_idx + 1
        );
        let work_email = format!("{email_local}@{email_domain}");
        let personal_local = format!(
            "{}{}{}",
            sanitize_sam_token(&legal_first)
                .chars()
                .take(1)
                .collect::<String>(),
            sanitize_sam_token(&legal_last)
                .chars()
                .take(3)
                .collect::<String>(),
            person_idx + 1
        );
        let personal_email = format!("{personal_local}@mail.org");

        if matches!(workday_status_code.as_str(), "TERM" | "LOA") && academic_status != "ENROLLED"
            || matches!(clinical_license_status.as_str(), "SUSPENDED" | "EXPIRED")
            || leave_code == "GLV"
        {
            ad_enabled = false;
        }

        plans_by_key.insert(
            pkey.clone(),
            PersonPlan {
                person_key: pkey,
                badge_id,
                legal_first,
                legal_last,
                pref_first,
                middle_name,
                dept_code,
                dept_canonical,
                job_code_raw,
                office_code,
                cost_center_raw,
                raw_country: raw_country.to_owned(),
                iso_country: iso_country.to_owned(),
                manager_key,
                sponsor_key,
                workday_status_code,
                vms_status_code,
                leave_code,
                legal_hold_raw,
                contract_end_date,
                clinical_license_status,
                academic_status,
                workday_aff_code,
                sec_aff_code,
                vms_aff_code,
                in_workday,
                in_secondary,
                in_vms,
                work_email,
                personal_email,
                has_ad_account,
                ad_enabled,
                ad_legal_hold_attr,
                ad_use_acquired_domain,
                ad_force_sam,
                blank_vms_dept_and_office,
                blank_workday_dept_fallback_to_vms,
                office_fallback_site_unmapped,
                manager_tbd_placeholder_sources,
                sec_bridge_personal_email_only,
                vms_bridge_personal_email_only,
                paired_withdrawn_override,
                f11_legal_cohort_spike_target,
                wd_academic_status_replica,
                wd_payroll_code_raw,
                vms_unapproved_revision,
                omit_spoke_badge,
                has_sap_account,
                has_epic_account,
                has_aws_account,
                has_custom_rest_account,
                extra_ad_groups,
                extra_entra_groups,
                extra_sap_groups,
                extra_aws_groups,
                extra_custom_rest_groups,
                has_adm_secondary,
                adm_extra_ad_groups,
                oauth_consents,
            },
        );
    }

    PersonPlanBuild {
        plans_by_key,
        used_sams,
        first_pool,
        last_pool,
        tomb_base_sam,
        long_base_sam,
        loa_4hop_owner,
    }
}

/// Top up dual-source secondary/VMS rows and designate `personal_email`-only bridge identities.
pub(super) fn top_up_dual_sources(
    ctx: &WorldCtx<'_>,
    plans_by_key: &mut IndexMap<String, PersonPlan>,
) {
    let WorldCtx {
        spec,
        canonical_person_keys,
        ..
    } = *ctx;
    let Personas {
        active_dir_1,
        active_dir_2,
        ..
    } = ctx.personas.clone();
    // Include enough dual-source rows for badge and personal-email fallback controls.
    let target_sec_real = match spec.tier {
        DifficultyTierV52::Focused => 6usize,
        DifficultyTierV52::MultiSystem => 9usize,
        DifficultyTierV52::FullStack => 11usize,
    };
    let target_vms_real = match spec.tier {
        DifficultyTierV52::Focused => 6usize,
        DifficultyTierV52::MultiSystem => 8usize,
        DifficultyTierV52::FullStack => 10usize,
    };
    let mut sec_real_count = plans_by_key.values().filter(|p| p.in_secondary).count();
    let mut vms_real_count = plans_by_key.values().filter(|p| p.in_vms).count();
    let target_sec_bridges = if spec.tier == DifficultyTierV52::Focused {
        0usize
    } else {
        4usize
    };
    let target_vms_bridges = match spec.tier {
        DifficultyTierV52::Focused => 0usize,
        DifficultyTierV52::MultiSystem => 3usize,
        DifficultyTierV52::FullStack => 4usize,
    };
    let mut sec_bridges_assigned = plans_by_key
        .values()
        .filter(|p| p.sec_bridge_personal_email_only)
        .count();
    let mut vms_bridges_assigned = 0usize;

    for pkey in canonical_person_keys {
        if let Some(p) = plans_by_key.get_mut(pkey) {
            let eligible_dual_fte = p.in_workday
                && p.has_ad_account
                && p.workday_status_code == "ACT"
                && p.leave_code.is_empty()
                && p.legal_hold_raw != "YES"
                && p.clinical_license_status.is_empty()
                && p.academic_status.is_empty()
                && !p.blank_workday_dept_fallback_to_vms
                && !p.office_fallback_site_unmapped
                && p.manager_tbd_placeholder_sources == 0;

            if eligible_dual_fte
                && !p.in_secondary
                && (sec_real_count < target_sec_real || sec_bridges_assigned < target_sec_bridges)
            {
                p.in_secondary = true;
                p.sec_aff_code.clear();
                sec_real_count += 1;
                if sec_bridges_assigned < target_sec_bridges {
                    p.sec_bridge_personal_email_only = true;
                    sec_bridges_assigned += 1;
                }
            } else if eligible_dual_fte
                && !p.in_vms
                && (vms_real_count < target_vms_real || vms_bridges_assigned < target_vms_bridges)
            {
                p.in_vms = true;
                p.vms_status_code = "ACT".to_owned();
                p.vms_aff_code.clear();
                p.contract_end_date = "2027-06-30".to_owned();
                p.sponsor_key = if vms_real_count % 2 == 0 {
                    active_dir_1.clone()
                } else {
                    active_dir_2.clone()
                };
                p.blank_vms_dept_and_office = false;
                vms_real_count += 1;
                if vms_bridges_assigned < target_vms_bridges {
                    p.vms_bridge_personal_email_only = true;
                    vms_bridges_assigned += 1;
                }
            }
        }
    }
}
