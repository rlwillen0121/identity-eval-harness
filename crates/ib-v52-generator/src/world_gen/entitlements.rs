//! Per-person resolved affiliations and spoke entitlement sets (birthright overlap bands).

use std::collections::HashSet;

use ib_v52_schemas::AffiliationTypeV52;
use indexmap::IndexMap;

use super::WorldCtx;
use super::group_ids::RoleGroupIds;
use super::identities::PersonPlan;
use super::types::GroupRoleTagV52;

/// Resolved canonical affiliation and lifecycle state per person.
pub(super) struct ResolvedPersonMeta {
    pub(super) primary_aff: AffiliationTypeV52,
    pub(super) active_affs: Vec<AffiliationTypeV52>,
    pub(super) is_active_person: bool,
}

/// Resolved person metadata plus per-spoke entitlement sets keyed by person key.
pub(super) struct EntitlementBuild {
    pub(super) meta_by_person: IndexMap<String, ResolvedPersonMeta>,
    pub(super) ad_ents_map: IndexMap<String, Vec<String>>,
    pub(super) entra_ents_map: IndexMap<String, Vec<String>>,
    pub(super) sap_ents_map: IndexMap<String, Vec<String>>,
    pub(super) epic_ents_map: IndexMap<String, Vec<String>>,
    pub(super) aws_ents_map: IndexMap<String, Vec<String>>,
    pub(super) rest_ents_map: IndexMap<String, Vec<String>>,
}

/// Push `val` unless it is empty or already present.
pub(super) fn push_uniq(vec: &mut Vec<String>, val: String) {
    if !val.is_empty() && !vec.contains(&val) {
        vec.push(val);
    }
}

/// Pass 1 (resolved affiliations & Active cohorts) and Pass 2 (entitlement sets).
pub(super) fn assign_entitlements(
    ctx: &WorldCtx<'_>,
    plans_by_key: &IndexMap<String, PersonPlan>,
) -> EntitlementBuild {
    let WorldCtx {
        spec,
        hardening,
        client,
        groups,
        canonical_person_keys,
        is_multi_or_full,
        ..
    } = *ctx;
    let RoleGroupIds {
        nested_eng_squad,
        nested_fin_squad,
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
        ..
    } = ctx.group_ids.clone();
    let parse_aff_str = |s: &str| -> Option<AffiliationTypeV52> {
        match s {
            "fte" => Some(AffiliationTypeV52::Fte),
            "part_time_staff" => Some(AffiliationTypeV52::PartTimeStaff),
            "faculty_full_time" => Some(AffiliationTypeV52::FacultyFullTime),
            "adjunct_faculty" => Some(AffiliationTypeV52::AdjunctFaculty),
            "seasonal_worker" => Some(AffiliationTypeV52::SeasonalWorker),
            "student_worker" => Some(AffiliationTypeV52::StudentWorker),
            "student" => Some(AffiliationTypeV52::Student),
            "clinical_provider" => Some(AffiliationTypeV52::ClinicalProvider),
            "clinical_resident" => Some(AffiliationTypeV52::ClinicalResident),
            "contingent_worker" => Some(AffiliationTypeV52::ContingentWorker),
            "vendor" => Some(AffiliationTypeV52::Vendor),
            "b2b_guest" => Some(AffiliationTypeV52::B2bGuest),
            "affiliate" => Some(AffiliationTypeV52::Affiliate),
            _ => None,
        }
    };
    let aff_prec = crate::archetypes::canonical_affiliation_precedence();
    let all_emp_affs = [
        AffiliationTypeV52::Fte,
        AffiliationTypeV52::PartTimeStaff,
        AffiliationTypeV52::FacultyFullTime,
        AffiliationTypeV52::AdjunctFaculty,
        AffiliationTypeV52::SeasonalWorker,
        AffiliationTypeV52::StudentWorker,
        AffiliationTypeV52::ClinicalProvider,
        AffiliationTypeV52::ClinicalResident,
    ];

    // Pass 1: Resolve exact canonical active affiliations & lifecycle active flag per person,
    //         and collect exact Active correlated cohort lists so Pass 2 hits precise overlap bands!
    let mut meta_by_person: IndexMap<String, ResolvedPersonMeta> = IndexMap::new();

    let mut coh_all_emp: Vec<String> = Vec::new();
    let mut coh_fin: Vec<String> = Vec::new();
    let mut coh_eng: Vec<String> = Vec::new();
    let mut coh_it: Vec<String> = Vec::new();
    let mut coh_clin: Vec<String> = Vec::new();
    let mut coh_acad: Vec<String> = Vec::new();
    let mut coh_leg: Vec<String> = Vec::new();
    let mut coh_stu: Vec<String> = Vec::new();
    let mut coh_cwk: Vec<String> = Vec::new();

    for pkey in canonical_person_keys {
        let p = &plans_by_key[pkey];
        let mut active_affs: Vec<AffiliationTypeV52> = Vec::new();
        if p.in_workday
            && !(p.workday_aff_code == "STW" && p.workday_status_code == "TERM")
            && let Some(mapped) = client.workday_aff_lookup.get(&p.workday_aff_code)
            && let Some(aff) = parse_aff_str(mapped)
            && !active_affs.contains(&aff)
        {
            active_affs.push(aff);
        }
        if p.in_secondary
            && let Some(mapped) = client.secondary_aff_lookup.get(&p.sec_aff_code)
            && let Some(aff) = parse_aff_str(mapped)
            && !active_affs.contains(&aff)
        {
            active_affs.push(aff);
        }
        if p.in_vms
            && let Some(mapped) = client.vms_aff_lookup.get(&p.vms_aff_code)
            && let Some(aff) = parse_aff_str(mapped)
            && !active_affs.contains(&aff)
        {
            active_affs.push(aff);
        }
        let is_enrolled = p.academic_status == "ENROLLED";
        if spec.preserve_active_student_on_worker_end
            && is_enrolled
            && !active_affs.contains(&AffiliationTypeV52::Student)
        {
            active_affs.push(AffiliationTypeV52::Student);
        }
        active_affs.sort_by_key(|a| {
            (
                aff_prec.iter().position(|x| x == a).unwrap_or(usize::MAX),
                a.clone(),
            )
        });
        if !spec.allow_additive_affiliations && active_affs.len() > 1 {
            let primary = active_affs[0].clone();
            let retain_stu = spec.preserve_active_student_on_worker_end
                && is_enrolled
                && primary != AffiliationTypeV52::Student
                && active_affs.contains(&AffiliationTypeV52::Student);
            active_affs
                .retain(|a| *a == primary || (retain_stu && *a == AffiliationTypeV52::Student));
        }
        let primary_aff = active_affs
            .first()
            .cloned()
            .unwrap_or(AffiliationTypeV52::Affiliate);

        let is_cwk_expired = (active_affs.contains(&AffiliationTypeV52::ContingentWorker)
            || active_affs.contains(&AffiliationTypeV52::B2bGuest))
            && !p.contract_end_date.is_empty()
            && p.contract_end_date < client.cutover_date
            && p.workday_status_code != "TERM";

        let is_active_person = p.legal_hold_raw != "YES"
            && p.leave_code != "GLV"
            && p.clinical_license_status != "SUSPENDED"
            && p.clinical_license_status != "EXPIRED"
            && !(p.workday_status_code == "TERM" && !is_enrolled)
            && !is_cwk_expired
            && p.leave_code != "FUR"
            && p.workday_status_code != "LOA"
            && p.workday_status_code != "PRE"
            && !active_affs.is_empty();

        if p.has_ad_account && is_active_person {
            if active_affs.iter().any(|a| all_emp_affs.contains(a)) {
                coh_all_emp.push(pkey.clone());
            }
            if active_affs.iter().any(|a| {
                matches!(
                    a,
                    AffiliationTypeV52::Fte | AffiliationTypeV52::PartTimeStaff
                )
            }) {
                match p.dept_canonical.as_str() {
                    "Finance" => coh_fin.push(pkey.clone()),
                    "Engineering" => coh_eng.push(pkey.clone()),
                    "Information Technology" => coh_it.push(pkey.clone()),
                    _ => {}
                }
            }
            if active_affs.iter().any(|a| {
                matches!(
                    a,
                    AffiliationTypeV52::ClinicalProvider | AffiliationTypeV52::ClinicalResident
                )
            }) && p.dept_canonical == "Clinical Operations"
            {
                coh_clin.push(pkey.clone());
            }
            if active_affs.iter().any(|a| {
                matches!(
                    a,
                    AffiliationTypeV52::FacultyFullTime | AffiliationTypeV52::AdjunctFaculty
                )
            }) && p.dept_canonical == "Academic Affairs"
            {
                coh_acad.push(pkey.clone());
            }
            if active_affs.contains(&AffiliationTypeV52::Fte) && p.dept_canonical == "Legal" {
                coh_leg.push(pkey.clone());
            }
            if active_affs.contains(&AffiliationTypeV52::Student) {
                coh_stu.push(pkey.clone());
            }
            if active_affs.iter().any(|a| {
                matches!(
                    a,
                    AffiliationTypeV52::ContingentWorker | AffiliationTypeV52::Vendor
                )
            }) {
                coh_cwk.push(pkey.clone());
            }
        }

        meta_by_person.insert(
            pkey.clone(),
            ResolvedPersonMeta {
                primary_aff,
                active_affs,
                is_active_person,
            },
        );
    }

    // Pass 2: Deterministic per-person spoke entitlement sets enforcing exact Wave 4 overlap statistics:
    // - MultiSystem & FullStack:
    //   * True birthright entitlements: 85-94% of Active target-affiliation peers (1-2 recent joiners omitted),
    //     0% of non-Active/wrong-aff accounts -> raw unfiltered AD dept overlap drops to ~45-70% (< 80%)!
    //   * Near-threshold decoy prod application_entitlements: 66-74% of Active target-affiliation peers
    //     PLUS 100% of non-Active/wrong-aff/dept-service accounts -> raw unfiltered AD dept overlap jumps >= 81%!
    // - Focused:
    //   * True birthright entitlements: 100% of Active target-affiliation peers; decoys: ~40% (< 60%).
    let mut ad_ents_map: IndexMap<String, Vec<String>> = IndexMap::new();
    let mut entra_ents_map: IndexMap<String, Vec<String>> = IndexMap::new();
    let mut sap_ents_map: IndexMap<String, Vec<String>> = IndexMap::new();
    let mut epic_ents_map: IndexMap<String, Vec<String>> = IndexMap::new();
    let mut aws_ents_map: IndexMap<String, Vec<String>> = IndexMap::new();
    let mut rest_ents_map: IndexMap<String, Vec<String>> = IndexMap::new();

    let add_ent_for_spoke =
        |pkey: &str,
         spoke: &str,
         gid: &str,
         ad_m: &mut IndexMap<String, Vec<String>>,
         entra_m: &mut IndexMap<String, Vec<String>>,
         sap_m: &mut IndexMap<String, Vec<String>>,
         epic_m: &mut IndexMap<String, Vec<String>>,
         aws_m: &mut IndexMap<String, Vec<String>>,
         rest_m: &mut IndexMap<String, Vec<String>>| {
            if gid.is_empty() {
                return;
            }
            match spoke {
                "ad" => push_uniq(ad_m.entry(pkey.to_owned()).or_default(), gid.to_owned()),
                "entra" => push_uniq(entra_m.entry(pkey.to_owned()).or_default(), gid.to_owned()),
                "sap" => push_uniq(sap_m.entry(pkey.to_owned()).or_default(), gid.to_owned()),
                "epic" => push_uniq(epic_m.entry(pkey.to_owned()).or_default(), gid.to_owned()),
                "aws" => push_uniq(aws_m.entry(pkey.to_owned()).or_default(), gid.to_owned()),
                "custom_rest" => push_uniq(
                    rest_m.entry(pkey.to_owned()).or_default(),
                    format!("{}{}", client.custom_rest_verb_prefix, gid),
                ),
                _ => {}
            }
        };

    let br_take_count = |len: usize| -> usize {
        if is_multi_or_full {
            // Keep small cohorts above the 80% boundary and larger cohorts near 90%.
            len - (len + 5).saturating_div(10).min(len / 5)
        } else {
            len
        }
    };
    let decoy_take_count = |len: usize, is_all_emp: bool| -> usize {
        if !is_multi_or_full {
            (len * 4) / 10
        } else if is_all_emp {
            (len * 73) / 100
        } else {
            (len * 70 + 40) / 100
        }
    };

    // Assign true birthright entitlements to 85-94% (MultiSystem/FullStack) or 100% (Focused) of each Active cohort.
    for pkey in coh_all_emp.iter().take(br_take_count(coh_all_emp.len())) {
        add_ent_for_spoke(
            pkey,
            "ad",
            &all_emp_group,
            &mut ad_ents_map,
            &mut entra_ents_map,
            &mut sap_ents_map,
            &mut epic_ents_map,
            &mut aws_ents_map,
            &mut rest_ents_map,
        );
        if is_multi_or_full {
            add_ent_for_spoke(
                pkey,
                "entra",
                &all_emp_entra_group,
                &mut ad_ents_map,
                &mut entra_ents_map,
                &mut sap_ents_map,
                &mut epic_ents_map,
                &mut aws_ents_map,
                &mut rest_ents_map,
            );
        }
    }
    let fin_take = br_take_count(coh_fin.len());
    for (idx, pkey) in coh_fin.iter().take(fin_take).enumerate() {
        let ad_fin_ent = if is_multi_or_full && idx * 100 < fin_take * 45 {
            &nested_fin_squad
        } else {
            &fin_group
        };
        add_ent_for_spoke(
            pkey,
            "ad",
            ad_fin_ent,
            &mut ad_ents_map,
            &mut entra_ents_map,
            &mut sap_ents_map,
            &mut epic_ents_map,
            &mut aws_ents_map,
            &mut rest_ents_map,
        );
        if is_multi_or_full {
            let fin_spoke = if spec.has_sap { "sap" } else { "entra" };
            add_ent_for_spoke(
                pkey,
                fin_spoke,
                &fin_spoke_group,
                &mut ad_ents_map,
                &mut entra_ents_map,
                &mut sap_ents_map,
                &mut epic_ents_map,
                &mut aws_ents_map,
                &mut rest_ents_map,
            );
        }
    }
    let eng_take = br_take_count(coh_eng.len());
    for (idx, pkey) in coh_eng.iter().take(eng_take).enumerate() {
        let ad_eng_ent = if is_multi_or_full && idx * 100 < eng_take * 45 {
            &nested_eng_squad
        } else {
            &eng_group
        };
        add_ent_for_spoke(
            pkey,
            "ad",
            ad_eng_ent,
            &mut ad_ents_map,
            &mut entra_ents_map,
            &mut sap_ents_map,
            &mut epic_ents_map,
            &mut aws_ents_map,
            &mut rest_ents_map,
        );
        if is_multi_or_full {
            let eng_spoke = if spec.has_aws { "aws" } else { "custom_rest" };
            add_ent_for_spoke(
                pkey,
                eng_spoke,
                &eng_spoke_group,
                &mut ad_ents_map,
                &mut entra_ents_map,
                &mut sap_ents_map,
                &mut epic_ents_map,
                &mut aws_ents_map,
                &mut rest_ents_map,
            );
        }
    }
    for pkey in coh_it.iter().take(br_take_count(coh_it.len())) {
        add_ent_for_spoke(
            pkey,
            "ad",
            &it_group,
            &mut ad_ents_map,
            &mut entra_ents_map,
            &mut sap_ents_map,
            &mut epic_ents_map,
            &mut aws_ents_map,
            &mut rest_ents_map,
        );
        if is_multi_or_full {
            add_ent_for_spoke(
                pkey,
                "custom_rest",
                &it_spoke_group,
                &mut ad_ents_map,
                &mut entra_ents_map,
                &mut sap_ents_map,
                &mut epic_ents_map,
                &mut aws_ents_map,
                &mut rest_ents_map,
            );
        }
    }
    for pkey in coh_clin.iter().take(br_take_count(coh_clin.len())) {
        let clin_spoke = if spec.has_epic { "epic" } else { "ad" };
        add_ent_for_spoke(
            pkey,
            clin_spoke,
            &clin_group,
            &mut ad_ents_map,
            &mut entra_ents_map,
            &mut sap_ents_map,
            &mut epic_ents_map,
            &mut aws_ents_map,
            &mut rest_ents_map,
        );
    }
    for pkey in coh_acad.iter().take(br_take_count(coh_acad.len())) {
        add_ent_for_spoke(
            pkey,
            "ad",
            &acad_group,
            &mut ad_ents_map,
            &mut entra_ents_map,
            &mut sap_ents_map,
            &mut epic_ents_map,
            &mut aws_ents_map,
            &mut rest_ents_map,
        );
    }
    for pkey in coh_leg.iter().take(br_take_count(coh_leg.len())) {
        add_ent_for_spoke(
            pkey,
            "ad",
            &leg_group,
            &mut ad_ents_map,
            &mut entra_ents_map,
            &mut sap_ents_map,
            &mut epic_ents_map,
            &mut aws_ents_map,
            &mut rest_ents_map,
        );
    }
    for pkey in coh_stu.iter().take(br_take_count(coh_stu.len())) {
        add_ent_for_spoke(
            pkey,
            "entra",
            &stu_group,
            &mut ad_ents_map,
            &mut entra_ents_map,
            &mut sap_ents_map,
            &mut epic_ents_map,
            &mut aws_ents_map,
            &mut rest_ents_map,
        );
    }
    for pkey in coh_cwk.iter().take(br_take_count(coh_cwk.len())) {
        add_ent_for_spoke(
            pkey,
            "ad",
            &cwk_group,
            &mut ad_ents_map,
            &mut entra_ents_map,
            &mut sap_ents_map,
            &mut epic_ents_map,
            &mut aws_ents_map,
            &mut rest_ents_map,
        );
    }

    // 100% NonProd DEV/UAT twins on Active Eng & IT peers (must be excluded because NonProd!).
    let _ = &stage_eng_twin;
    for pkey in &coh_eng {
        add_ent_for_spoke(
            pkey,
            "ad",
            &nonprod_group,
            &mut ad_ents_map,
            &mut entra_ents_map,
            &mut sap_ents_map,
            &mut epic_ents_map,
            &mut aws_ents_map,
            &mut rest_ents_map,
        );
    }
    for pkey in &coh_it {
        add_ent_for_spoke(
            pkey,
            "ad",
            &uat_it_twin,
            &mut ad_ents_map,
            &mut entra_ents_map,
            &mut sap_ents_map,
            &mut epic_ents_map,
            &mut aws_ents_map,
            &mut rest_ents_map,
        );
    }

    // Assign Near-Threshold Decoy PROD application_entitlement groups!
    // Active cohort receives 66-74% (MultiSystem/FullStack) or 40% (Focused).
    type DeptDecoySet<'a> = (&'a str, &'a [String], &'a [(String, String)]);
    let dept_decoy_sets: [DeptDecoySet<'_>; 5] = [
        ("Finance", &coh_fin, &decoys_fin),
        ("Engineering", &coh_eng, &decoys_eng),
        ("Information Technology", &coh_it, &decoys_it),
        ("Clinical Operations", &coh_clin, &decoys_clin),
        ("Academic Affairs", &coh_acad, &decoys_acad),
    ];
    for (dept_name, cohort, decoys) in dept_decoy_sets {
        let active_set: HashSet<&str> = cohort.iter().map(String::as_str).collect();
        let d_take = decoy_take_count(cohort.len(), false);
        for (gid, spoke) in decoys {
            for pkey in cohort.iter().take(d_take) {
                add_ent_for_spoke(
                    pkey,
                    spoke,
                    gid,
                    &mut ad_ents_map,
                    &mut entra_ents_map,
                    &mut sap_ents_map,
                    &mut epic_ents_map,
                    &mut aws_ents_map,
                    &mut rest_ents_map,
                );
            }
            // On MultiSystem & FullStack, give 100% of non-Active & wrong-affiliation AD accounts
            // in the SAME department this decoy so raw unfiltered AD dept overlap jumps >= 81%!
            if is_multi_or_full {
                let mut active_stu_holders = 0usize;
                let max_stu_holders = (coh_stu.len() * 65) / 100;
                for pkey in canonical_person_keys {
                    let p = &plans_by_key[pkey];
                    if !p.has_ad_account || p.dept_canonical != dept_name {
                        continue;
                    }
                    if active_set.contains(pkey.as_str()) {
                        continue;
                    }
                    let meta = &meta_by_person[pkey];
                    if meta.is_active_person
                        && meta.active_affs.contains(&AffiliationTypeV52::Student)
                    {
                        if active_stu_holders >= max_stu_holders {
                            continue;
                        }
                        active_stu_holders += 1;
                    }
                    add_ent_for_spoke(
                        pkey,
                        spoke,
                        gid,
                        &mut ad_ents_map,
                        &mut entra_ents_map,
                        &mut sap_ents_map,
                        &mut epic_ents_map,
                        &mut aws_ents_map,
                        &mut rest_ents_map,
                    );
                }
            }
        }
    }

    // All-Employee near-threshold decoy (`CORP_TRAVEL`):
    // Stratified at 66-74% across EVERY department cohort (so no single department randomly exceeds 74%!)
    // PLUS 100% of all non-Active AD accounts + ~70% of Active CWK & ~66% of Active Student
    // + 100% of AD service accounts & AD orphans -> raw unfiltered AD overlap >= 80%!
    let cwk_all_emp_take = (coh_cwk.len() * 70) / 100;
    let stu_all_emp_take = (coh_stu.len() * 66) / 100;
    let dept_emp_cohorts: [&Vec<String>; 6] =
        [&coh_fin, &coh_eng, &coh_it, &coh_clin, &coh_acad, &coh_leg];
    let mut strat_seen: HashSet<&str> = HashSet::new();
    let mut all_emp_decoy_active_holders: Vec<String> = Vec::new();
    for coh in dept_emp_cohorts {
        let take_n = decoy_take_count(coh.len(), false);
        for (idx, pkey) in coh.iter().enumerate() {
            strat_seen.insert(pkey.as_str());
            if idx < take_n {
                all_emp_decoy_active_holders.push(pkey.clone());
            }
        }
    }
    let remaining_emp: Vec<&String> = coh_all_emp
        .iter()
        .filter(|pk| !strat_seen.contains(pk.as_str()))
        .collect();
    for pkey in remaining_emp
        .iter()
        .take(decoy_take_count(remaining_emp.len(), false))
    {
        all_emp_decoy_active_holders.push((*pkey).clone());
    }
    for (gid, spoke) in &decoys_all_emp {
        for pkey in &all_emp_decoy_active_holders {
            add_ent_for_spoke(
                pkey,
                spoke,
                gid,
                &mut ad_ents_map,
                &mut entra_ents_map,
                &mut sap_ents_map,
                &mut epic_ents_map,
                &mut aws_ents_map,
                &mut rest_ents_map,
            );
        }
        if is_multi_or_full {
            for pkey in canonical_person_keys {
                let p = &plans_by_key[pkey];
                if p.has_ad_account && !meta_by_person[pkey].is_active_person {
                    add_ent_for_spoke(
                        pkey,
                        spoke,
                        gid,
                        &mut ad_ents_map,
                        &mut entra_ents_map,
                        &mut sap_ents_map,
                        &mut epic_ents_map,
                        &mut aws_ents_map,
                        &mut rest_ents_map,
                    );
                }
            }
            for pkey in coh_cwk.iter().take(cwk_all_emp_take) {
                add_ent_for_spoke(
                    pkey,
                    spoke,
                    gid,
                    &mut ad_ents_map,
                    &mut entra_ents_map,
                    &mut sap_ents_map,
                    &mut epic_ents_map,
                    &mut aws_ents_map,
                    &mut rest_ents_map,
                );
            }
            for pkey in coh_stu.iter().take(stu_all_emp_take) {
                add_ent_for_spoke(
                    pkey,
                    spoke,
                    gid,
                    &mut ad_ents_map,
                    &mut entra_ents_map,
                    &mut sap_ents_map,
                    &mut epic_ents_map,
                    &mut aws_ents_map,
                    &mut rest_ents_map,
                );
            }
        }
    }

    // `F06-CR` (Appendix C): memberships of the CMDB-retired Expense Classic group were never cleaned
    // up, so every active Finance FTE/PT account still holds it (100% >= 80% mining threshold).
    if hardening.cmdb_retirement_register {
        let populated_groups = groups.iter().filter(|g| {
            matches!(
                g.role_tag,
                GroupRoleTagV52::RetiredAppPopulated | GroupRoleTagV52::RetiredAppBridge
            )
        });
        for g in populated_groups {
            for pkey in &coh_fin {
                push_uniq(
                    ad_ents_map.entry(pkey.clone()).or_default(),
                    g.record.group_id.clone(),
                );
            }
        }
    }

    EntitlementBuild {
        meta_by_person,
        ad_ents_map,
        entra_ents_map,
        sap_ents_map,
        epic_ents_map,
        aws_ents_map,
        rest_ents_map,
    }
}
