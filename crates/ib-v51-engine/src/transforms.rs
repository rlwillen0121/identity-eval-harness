//! Declarative attribute transform AST evaluator and string/date normalization utilities.

use std::collections::HashSet;

use ib_v51_schemas::{
    AffiliationTypeV51, CompareOpV51, DateCompareOpV51, GroupDispositionV51, LifecycleStateIdV51,
    RawSourceRecordV51, TransformExprV51, TransformsConfigV51,
};
use indexmap::IndexMap;
use regex::Regex;

pub const MAX_TRANSFORM_DEPTH: usize = 32;

/// Evaluation context for declarative transforms on a single person's matched authoritative source records.
pub struct TransformEvalContext<'a> {
    pub transforms: &'a TransformsConfigV51,
    pub declared_sources: &'a HashSet<String>,
    pub matched_records: &'a IndexMap<String, &'a RawSourceRecordV51>,
    pub cutover_date: &'a str,
}

/// Evaluate a declarative `TransformExprV51` with cycle detection and max-depth enforcement (`32`).
pub fn eval_transform_expr(
    expr: &TransformExprV51,
    ctx: &TransformEvalContext<'_>,
) -> Result<String, String> {
    let mut visiting = HashSet::new();
    eval_inner(expr, ctx, 0, &mut visiting)
}

fn eval_inner(
    expr: &TransformExprV51,
    ctx: &TransformEvalContext<'_>,
    depth: usize,
    visiting: &mut HashSet<String>,
) -> Result<String, String> {
    if depth > MAX_TRANSFORM_DEPTH {
        return Err(format!(
            "transform evaluation exceeded maximum recursion depth ({MAX_TRANSFORM_DEPTH})"
        ));
    }

    match expr {
        TransformExprV51::SourceAttr { source_id, field } => {
            if !ctx.declared_sources.contains(source_id) {
                return Ok(String::new());
            }
            Ok(ctx
                .matched_records
                .get(source_id)
                .and_then(|rec| rec.attributes.get(field))
                .cloned()
                .unwrap_or_default())
        }
        TransformExprV51::Literal { value } => Ok(value.clone()),
        TransformExprV51::RefTransform { name } => {
            if !visiting.insert(name.clone()) {
                return Err(format!("cyclic transform reference detected at '{name}'"));
            }
            let target = ctx
                .transforms
                .transforms
                .get(name)
                .ok_or_else(|| format!("unknown transform reference '{name}'"))?;
            let result = eval_inner(target, ctx, depth + 1, visiting);
            visiting.remove(name);
            result
        }
        TransformExprV51::FirstValid {
            values,
            ignore_blank,
        } => {
            for candidate in values {
                let evaluated = eval_inner(candidate, ctx, depth + 1, visiting)?;
                let valid = if *ignore_blank {
                    !evaluated.trim().is_empty()
                } else {
                    !evaluated.is_empty()
                };
                if valid {
                    return Ok(evaluated);
                }
            }
            Ok(String::new())
        }
        TransformExprV51::Lookup {
            input,
            table,
            default_value,
        } => {
            let key = eval_inner(input, ctx, depth + 1, visiting)?;
            if let Some(mapped) = table.get(&key) {
                return Ok(mapped.clone());
            }
            if let Some(default_expr) = default_value {
                eval_inner(default_expr, ctx, depth + 1, visiting)
            } else {
                Ok(String::new())
            }
        }
        TransformExprV51::Concat { parts } => {
            let mut out = String::new();
            for part in parts {
                out.push_str(&eval_inner(part, ctx, depth + 1, visiting)?);
            }
            Ok(out)
        }
        TransformExprV51::Trim { input } => Ok(eval_inner(input, ctx, depth + 1, visiting)?
            .trim()
            .to_owned()),
        TransformExprV51::Lower { input } => {
            Ok(eval_inner(input, ctx, depth + 1, visiting)?.to_lowercase())
        }
        TransformExprV51::Upper { input } => {
            Ok(eval_inner(input, ctx, depth + 1, visiting)?.to_uppercase())
        }
        TransformExprV51::StripDiacritics { input } => {
            let raw = eval_inner(input, ctx, depth + 1, visiting)?;
            Ok(strip_diacritics(&raw))
        }
        TransformExprV51::RegexReplace {
            input,
            pattern,
            replacement,
        } => {
            let raw = eval_inner(input, ctx, depth + 1, visiting)?;
            let re = Regex::new(pattern)
                .map_err(|err| format!("invalid regex_replace pattern '{pattern}': {err}"))?;
            Ok(re.replace_all(&raw, replacement.as_str()).into_owned())
        }
        TransformExprV51::Substring {
            input,
            start,
            max_len,
        } => {
            let raw = eval_inner(input, ctx, depth + 1, visiting)?;
            Ok(raw.chars().skip(*start).take(*max_len).collect())
        }
        TransformExprV51::IsoCountry2 { input } => {
            let raw = eval_inner(input, ctx, depth + 1, visiting)?;
            Ok(iso_country_2(&raw))
        }
        TransformExprV51::Conditional {
            left,
            cmp,
            right,
            if_true,
            if_false,
        } => {
            let left_val = eval_inner(left, ctx, depth + 1, visiting)?;
            let right_val = match right {
                Some(r) => Some(eval_inner(r, ctx, depth + 1, visiting)?),
                None => None,
            };
            let matched = eval_compare_op(&left_val, *cmp, right_val.as_deref());
            if matched {
                eval_inner(if_true, ctx, depth + 1, visiting)
            } else {
                eval_inner(if_false, ctx, depth + 1, visiting)
            }
        }
        TransformExprV51::DateCompare {
            left,
            cmp,
            right,
            if_true,
            if_false,
        } => {
            let left_val = eval_inner(left, ctx, depth + 1, visiting)?;
            let right_val = eval_inner(right, ctx, depth + 1, visiting)?;
            let matched = eval_date_compare(&left_val, *cmp, &right_val, ctx.cutover_date);
            if matched {
                eval_inner(if_true, ctx, depth + 1, visiting)
            } else {
                eval_inner(if_false, ctx, depth + 1, visiting)
            }
        }
    }
}

/// Evaluate a `CompareOpV51` predicate between `left` and optional `right`.
#[must_use]
pub fn eval_compare_op(left: &str, cmp: CompareOpV51, right: Option<&str>) -> bool {
    let rhs = right.unwrap_or("");
    match cmp {
        CompareOpV51::Eq => left == rhs,
        CompareOpV51::Ne => left != rhs,
        CompareOpV51::Contains => !rhs.is_empty() && left.contains(rhs),
        CompareOpV51::StartsWith => left.starts_with(rhs),
        CompareOpV51::EndsWith => left.ends_with(rhs),
        CompareOpV51::InList => !rhs.is_empty() && rhs.split('|').any(|item| left == item),
        CompareOpV51::IsEmpty => left.trim().is_empty(),
        CompareOpV51::IsNotEmpty => !left.trim().is_empty(),
    }
}

/// Evaluate a `DateCompareOpV51` predicate (`"CUTOVER_DATE"` resolves to `cutover_date`).
/// Returns `false` if either side is empty after trimming.
#[must_use]
pub fn eval_date_compare(
    left_raw: &str,
    cmp: DateCompareOpV51,
    right_raw: &str,
    cutover_date: &str,
) -> bool {
    let left_trimmed = left_raw.trim();
    let right_trimmed = right_raw.trim();
    if left_trimmed.is_empty() || right_trimmed.is_empty() {
        return false;
    }
    let left = if left_trimmed == "CUTOVER_DATE" {
        cutover_date.trim()
    } else {
        left_trimmed
    };
    let right = if right_trimmed == "CUTOVER_DATE" {
        cutover_date.trim()
    } else {
        right_trimmed
    };
    if left.is_empty() || right.is_empty() {
        return false;
    }

    if let (Some(l_epoch), Some(r_epoch)) = (parse_date_or_epoch(left), parse_date_or_epoch(right))
    {
        return match cmp {
            DateCompareOpV51::Lt => l_epoch < r_epoch,
            DateCompareOpV51::Le => l_epoch <= r_epoch,
            DateCompareOpV51::Gt => l_epoch > r_epoch,
            DateCompareOpV51::Ge => l_epoch >= r_epoch,
            DateCompareOpV51::Eq => l_epoch == r_epoch,
        };
    }

    match cmp {
        DateCompareOpV51::Lt => left < right,
        DateCompareOpV51::Le => left <= right,
        DateCompareOpV51::Gt => left > right,
        DateCompareOpV51::Ge => left >= right,
        DateCompareOpV51::Eq => left == right,
    }
}

fn parse_date_or_epoch(s: &str) -> Option<i64> {
    if let Ok(epoch) = s.parse::<i64>() {
        return Some(epoch);
    }
    // Parse YYYY-MM-DD (ignoring optional time suffix starting with 'T' or ' ')
    let date_part = s.split(['T', ' ']).next().unwrap_or(s);
    let mut parts = date_part.split('-');
    let year: i64 = parts.next()?.parse().ok()?;
    let month: i64 = parts.next()?.parse().ok()?;
    let day: i64 = parts.next()?.parse().ok()?;
    if parts.next().is_some() || !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    Some(days_from_civil(year, month, day) * 86_400)
}

/// Howard Hinnant's civil-from-days inverse: days since 1970-01-01.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let m_adj = if month > 2 { month - 3 } else { month + 9 };
    let doy = (153 * m_adj + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// Full Latin / European diacritic transliteration to ASCII.
#[must_use]
pub fn strip_diacritics(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for ch in input.chars() {
        // Drop Unicode combining diacritical marks if NFD-decomposed input appears.
        if ('\u{0300}'..='\u{036F}').contains(&ch) {
            continue;
        }
        match ch {
            'á' | 'à' | 'â' | 'ä' | 'ã' | 'å' | 'ā' | 'ă' | 'ą' => out.push('a'),
            'Á' | 'À' | 'Â' | 'Ä' | 'Ã' | 'Å' | 'Ā' | 'Ă' | 'Ą' => out.push('A'),
            'æ' => out.push_str("ae"),
            'Æ' => out.push_str("AE"),
            'ç' | 'ć' | 'ĉ' | 'ċ' | 'č' => out.push('c'),
            'Ç' | 'Ć' | 'Ĉ' | 'Ċ' | 'Č' => out.push('C'),
            'ď' | 'đ' => out.push('d'),
            'Ď' | 'Đ' => out.push('D'),
            'é' | 'è' | 'ê' | 'ë' | 'ē' | 'ĕ' | 'ė' | 'ę' | 'ě' => out.push('e'),
            'É' | 'È' | 'Ê' | 'Ë' | 'Ē' | 'Ĕ' | 'Ė' | 'Ę' | 'Ě' => out.push('E'),
            'ĝ' | 'ğ' | 'ġ' | 'ģ' => out.push('g'),
            'Ĝ' | 'Ğ' | 'Ġ' | 'Ģ' => out.push('G'),
            'ĥ' | 'ħ' => out.push('h'),
            'Ĥ' | 'Ħ' => out.push('H'),
            'í' | 'ì' | 'î' | 'ï' | 'ĩ' | 'ī' | 'ĭ' | 'į' | 'ı' => out.push('i'),
            'Í' | 'Ì' | 'Î' | 'Ï' | 'Ĩ' | 'Ī' | 'Ĭ' | 'Į' | 'İ' => out.push('I'),
            'ĵ' => out.push('j'),
            'Ĵ' => out.push('J'),
            'ķ' => out.push('k'),
            'Ķ' => out.push('K'),
            'ĺ' | 'ļ' | 'ľ' | 'ŀ' | 'ł' => out.push('l'),
            'Ĺ' | 'Ļ' | 'Ľ' | 'Ŀ' | 'Ł' => out.push('L'),
            'ñ' | 'ń' | 'ņ' | 'ň' => out.push('n'),
            'Ñ' | 'Ń' | 'Ņ' | 'Ň' => out.push('N'),
            'ó' | 'ò' | 'ô' | 'ö' | 'õ' | 'ø' | 'ō' | 'ŏ' | 'ő' => out.push('o'),
            'Ó' | 'Ò' | 'Ô' | 'Ö' | 'Õ' | 'Ø' | 'Ō' | 'Ŏ' | 'Ő' => out.push('O'),
            'œ' => out.push_str("oe"),
            'Œ' => out.push_str("OE"),
            'ŕ' | 'ŗ' | 'ř' => out.push('r'),
            'Ŕ' | 'Ŗ' | 'Ř' => out.push('R'),
            'ś' | 'ŝ' | 'ş' | 'š' => out.push('s'),
            'Ś' | 'Ŝ' | 'Ş' | 'Š' => out.push('S'),
            'ß' => out.push_str("ss"),
            'ẞ' => out.push_str("SS"),
            'ţ' | 'ť' | 'ŧ' => out.push('t'),
            'Ţ' | 'Ť' | 'Ŧ' => out.push('T'),
            'ú' | 'ù' | 'û' | 'ü' | 'ũ' | 'ū' | 'ŭ' | 'ů' | 'ű' | 'ų' => out.push('u'),
            'Ú' | 'Ù' | 'Û' | 'Ü' | 'Ũ' | 'Ū' | 'Ŭ' | 'Ů' | 'Ű' | 'Ų' => out.push('U'),
            'ŵ' => out.push('w'),
            'Ŵ' => out.push('W'),
            'ý' | 'ÿ' | 'ŷ' => out.push('y'),
            'Ý' | 'Ÿ' | 'Ŷ' => out.push('Y'),
            'ź' | 'ż' | 'ž' => out.push('z'),
            'Ź' | 'Ż' | 'Ž' => out.push('Z'),
            other => out.push(other),
        }
    }
    out
}

/// Normalize country names or ISO-3 codes to 2-character ISO-3166-1 alpha-2 uppercase codes.
#[must_use]
pub fn iso_country_2(input: &str) -> String {
    let trimmed = input.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    let normalized = strip_diacritics(trimmed)
        .to_ascii_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");

    let mapped = match normalized.as_str() {
        "united states" | "united states of america" | "usa" | "u.s.a." | "u.s." | "us" => {
            Some("US")
        }
        "united kingdom" | "uk" | "u.k." | "great britain" | "britain" | "gbr" | "gb" => Some("GB"),
        "canada" | "can" | "ca" => Some("CA"),
        "germany" | "deutschland" | "deu" | "de" => Some("DE"),
        "france" | "fra" | "fr" => Some("FR"),
        "india" | "ind" | "in" => Some("IN"),
        "japan" | "jpn" | "jp" => Some("JP"),
        "australia" | "aus" | "au" => Some("AU"),
        "mexico" | "mex" | "mx" => Some("MX"),
        "brazil" | "brasil" | "bra" | "br" => Some("BR"),
        "ireland" | "irl" | "ie" => Some("IE"),
        "singapore" | "sgp" | "sg" => Some("SG"),
        "spain" | "espana" | "esp" | "es" => Some("ES"),
        "netherlands" | "the netherlands" | "holland" | "nld" | "nl" => Some("NL"),
        "switzerland" | "schweiz" | "suisse" | "che" | "ch" => Some("CH"),
        "poland" | "polska" | "pol" | "pl" => Some("PL"),
        _ => None,
    };

    if let Some(code) = mapped {
        return code.to_owned();
    }

    let chars: Vec<char> = trimmed.chars().collect();
    if chars.len() == 2 && chars.iter().all(char::is_ascii_alphabetic) {
        return trimmed.to_ascii_uppercase();
    }

    String::new()
}

/// Deterministic snake_case representation of `AffiliationTypeV51`.
#[must_use]
pub const fn affiliation_to_str(aff: &AffiliationTypeV51) -> &'static str {
    match aff {
        AffiliationTypeV51::Fte => "fte",
        AffiliationTypeV51::PartTimeStaff => "part_time_staff",
        AffiliationTypeV51::FacultyFullTime => "faculty_full_time",
        AffiliationTypeV51::AdjunctFaculty => "adjunct_faculty",
        AffiliationTypeV51::SeasonalWorker => "seasonal_worker",
        AffiliationTypeV51::StudentWorker => "student_worker",
        AffiliationTypeV51::Student => "student",
        AffiliationTypeV51::ClinicalProvider => "clinical_provider",
        AffiliationTypeV51::ClinicalResident => "clinical_resident",
        AffiliationTypeV51::ContingentWorker => "contingent_worker",
        AffiliationTypeV51::Vendor => "vendor",
        AffiliationTypeV51::Affiliate => "affiliate",
        AffiliationTypeV51::B2bGuest => "b2b_guest",
    }
}

/// Parse a snake_case `AffiliationTypeV51` token.
#[must_use]
pub fn parse_affiliation(token: &str) -> Option<AffiliationTypeV51> {
    match token.trim() {
        "fte" => Some(AffiliationTypeV51::Fte),
        "part_time_staff" => Some(AffiliationTypeV51::PartTimeStaff),
        "faculty_full_time" => Some(AffiliationTypeV51::FacultyFullTime),
        "adjunct_faculty" => Some(AffiliationTypeV51::AdjunctFaculty),
        "seasonal_worker" => Some(AffiliationTypeV51::SeasonalWorker),
        "student_worker" => Some(AffiliationTypeV51::StudentWorker),
        "student" => Some(AffiliationTypeV51::Student),
        "clinical_provider" => Some(AffiliationTypeV51::ClinicalProvider),
        "clinical_resident" => Some(AffiliationTypeV51::ClinicalResident),
        "contingent_worker" => Some(AffiliationTypeV51::ContingentWorker),
        "vendor" => Some(AffiliationTypeV51::Vendor),
        "affiliate" => Some(AffiliationTypeV51::Affiliate),
        "b2b_guest" => Some(AffiliationTypeV51::B2bGuest),
        _ => None,
    }
}

/// Deterministic snake_case representation of `LifecycleStateIdV51`.
#[must_use]
pub const fn lifecycle_state_to_str(state: LifecycleStateIdV51) -> &'static str {
    match state {
        LifecycleStateIdV51::LegalHold => "legal_hold",
        LifecycleStateIdV51::Terminated => "terminated",
        LifecycleStateIdV51::GardenLeave => "garden_leave",
        LifecycleStateIdV51::SuspendedCredential => "suspended_credential",
        LifecycleStateIdV51::ContingentExpiredGrace => "contingent_expired_grace",
        LifecycleStateIdV51::SeasonalFurlough => "seasonal_furlough",
        LifecycleStateIdV51::LeaveOfAbsence => "leave_of_absence",
        LifecycleStateIdV51::PreHire => "pre_hire",
        LifecycleStateIdV51::Active => "active",
        LifecycleStateIdV51::InactiveOrphan => "inactive_orphan",
    }
}

/// Deterministic snake_case representation of `GroupDispositionV51`.
#[must_use]
pub const fn group_disposition_to_str(disp: GroupDispositionV51) -> &'static str {
    match disp {
        GroupDispositionV51::ApplicationEntitlement => "application_entitlement",
        GroupDispositionV51::BirthrightBundleCandidate => "birthright_bundle_candidate",
        GroupDispositionV51::PrivilegedAdminRequestOnly => "privileged_admin_request_only",
        GroupDispositionV51::OrphanedDeprecatedDoNotAssign => "orphaned_deprecated_do_not_assign",
        GroupDispositionV51::ServiceAccountOnlyDoNotAssign => "service_account_only_do_not_assign",
    }
}
