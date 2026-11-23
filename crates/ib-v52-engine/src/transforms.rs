//! Declarative attribute transform AST evaluator and string/date normalization utilities.

use std::collections::HashSet;

use ib_v52_schemas::TRANSFORM_BUDGET_EXCEEDED;
use ib_v52_schemas::{
    AffiliationTypeV52, CompareOpV52, DateCompareOpV52, GroupDispositionV52, LifecycleStateIdV52,
    RawSourceRecordV52, TransformExprV52, TransformsConfigV52, json_pointer_child,
};
use indexmap::IndexMap;
use regex::Regex;

pub const MAX_TRANSFORM_DEPTH: usize = 32;

const MAX_NODES: usize = 2_500_000;
const MAX_STRING_BYTES: usize = 64 * 1024;
const MAX_TOTAL_BYTES: usize = 16 * 1024 * 1024;
const MAX_REGEX_BYTES: usize = 8 * 1024;
const MAX_REGEX_WORK: usize = 16 * 1024 * 1024;

#[derive(Clone, Copy)]
enum PointerSegment<'a> {
    Key(&'a str),
    KeyIndex(&'a str, usize),
}

struct EvalLocation<'a> {
    document: &'a str,
    base_pointer: &'a str,
    parent: Option<&'a EvalLocation<'a>>,
    segment: Option<PointerSegment<'a>>,
}

impl<'a> EvalLocation<'a> {
    fn root(document: &'a str, base_pointer: &'a str) -> Self {
        Self {
            document,
            base_pointer,
            parent: None,
            segment: None,
        }
    }

    fn child<'b>(&'b self, segment: PointerSegment<'b>) -> EvalLocation<'b> {
        EvalLocation {
            document: self.document,
            base_pointer: self.base_pointer,
            parent: Some(self),
            segment: Some(segment),
        }
    }

    fn pointer(&self) -> (String, String) {
        let mut parts = Vec::new();
        let mut current = self;
        while let Some(parent) = current.parent {
            if let Some(part) = current.segment {
                parts.push(part);
            }
            current = parent;
        }
        parts.reverse();

        let mut pointer = current.base_pointer.to_owned();
        for part in parts {
            pointer = match part {
                PointerSegment::Key(key) => json_pointer_child(&pointer, key),
                PointerSegment::KeyIndex(key, index) => {
                    let field_pointer = json_pointer_child(&pointer, key);
                    json_pointer_child(&field_pointer, &index.to_string())
                }
            };
        }
        (self.document.to_owned(), pointer)
    }

    fn with_child_pointer(&self, segment: PointerSegment<'_>) -> (String, String) {
        let (document, mut pointer) = self.pointer();
        pointer = match segment {
            PointerSegment::Key(key) => json_pointer_child(&pointer, key),
            PointerSegment::KeyIndex(key, index) => {
                let field_pointer = json_pointer_child(&pointer, key);
                json_pointer_child(&field_pointer, &index.to_string())
            }
        };
        (document, pointer)
    }
}

/// One allowance shared by all attribute and username transforms in a world pass.
#[derive(Default)]
pub(crate) struct TransformBudget {
    nodes: usize,
    total_bytes: usize,
    regex_work: usize,
    last_error_location: Option<(String, String)>,
    #[cfg(test)]
    peak_string: usize,
    #[cfg(test)]
    peak_regex: usize,
}

fn check_limit(used: usize, limit: usize) -> Result<(), String> {
    if used > limit {
        Err(TRANSFORM_BUDGET_EXCEEDED.to_owned())
    } else {
        Ok(())
    }
}

fn append(out: &mut String, part: &str) -> Result<(), String> {
    check_limit(out.len().saturating_add(part.len()), MAX_STRING_BYTES)?;
    out.push_str(part);
    Ok(())
}

impl TransformBudget {
    fn charge_regex(&mut self, work: usize) -> Result<(), String> {
        self.regex_work = self.regex_work.saturating_add(work);
        check_limit(self.regex_work, MAX_REGEX_WORK)
    }

    pub(crate) fn evaluate(
        &mut self,
        expr: &TransformExprV52,
        ctx: &TransformEvalContext<'_>,
    ) -> Result<String, String> {
        self.evaluate_at(expr, ctx, "output", "")
    }

    pub(crate) fn evaluate_at(
        &mut self,
        expr: &TransformExprV52,
        ctx: &TransformEvalContext<'_>,
        document: &str,
        pointer: &str,
    ) -> Result<String, String> {
        self.last_error_location = None;
        let expression_location = EvalLocation::root(document, pointer);
        eval_inner(
            expr,
            ctx,
            0,
            &mut HashSet::new(),
            self,
            &expression_location,
        )
    }

    pub(crate) fn evaluate_named_at(
        &mut self,
        name: &str,
        ctx: &TransformEvalContext<'_>,
        document: &str,
        pointer: &str,
    ) -> Result<String, String> {
        self.last_error_location = None;
        self.nodes += 1;
        if let Err(error) = check_limit(self.nodes, MAX_NODES) {
            self.last_error_location = None;
            return Err(error);
        }
        let Some(target) = ctx.transforms.transforms.get(name) else {
            self.last_error_location = Some((document.to_owned(), pointer.to_owned()));
            return Err(format!("unknown transform reference '{name}'"));
        };

        let mut visiting = HashSet::from([name.to_owned()]);
        let transforms_root = EvalLocation::root("config/transforms.json", "");
        let transforms_location = transforms_root.child(PointerSegment::Key("transforms"));
        let transform_location = transforms_location.child(PointerSegment::Key(name));
        eval_inner(target, ctx, 1, &mut visiting, self, &transform_location)
    }

    pub(crate) fn last_error_location(&self) -> Option<(&str, &str)> {
        self.last_error_location
            .as_ref()
            .map(|(document, pointer)| (document.as_str(), pointer.as_str()))
    }

    /// The string-based backend interface reserves one exact error for budget exhaustion.
    pub(crate) fn with_context(error: String, context: impl FnOnce() -> String) -> String {
        if error == TRANSFORM_BUDGET_EXCEEDED {
            error
        } else {
            format!("{}: {error}", context())
        }
    }
}

fn bounded_copy(value: &str) -> Result<String, String> {
    check_limit(value.len(), MAX_STRING_BYTES)?;
    Ok(value.to_owned())
}

/// Evaluation context for declarative transforms on a single person's matched authoritative source records.
pub struct TransformEvalContext<'a> {
    pub transforms: &'a TransformsConfigV52,
    pub declared_sources: &'a HashSet<String>,
    pub matched_records: &'a IndexMap<String, &'a RawSourceRecordV52>,
    pub cutover_date: &'a str,
}

/// Evaluate a declarative `TransformExprV52` with cycle detection and max-depth enforcement (`32`).
pub fn eval_transform_expr(
    expr: &TransformExprV52,
    ctx: &TransformEvalContext<'_>,
) -> Result<String, String> {
    TransformBudget::default().evaluate(expr, ctx)
}

fn eval_child(
    expr: &TransformExprV52,
    ctx: &TransformEvalContext<'_>,
    depth: usize,
    visiting: &mut HashSet<String>,
    budget: &mut TransformBudget,
    location: &EvalLocation<'_>,
    segment: PointerSegment<'_>,
) -> Result<String, String> {
    let child_location = location.child(segment);
    eval_inner(expr, ctx, depth + 1, visiting, budget, &child_location)
}

fn eval_inner(
    expr: &TransformExprV52,
    ctx: &TransformEvalContext<'_>,
    depth: usize,
    visiting: &mut HashSet<String>,
    budget: &mut TransformBudget,
    location: &EvalLocation<'_>,
) -> Result<String, String> {
    budget.nodes += 1;
    check_limit(budget.nodes, MAX_NODES)?;
    if depth > MAX_TRANSFORM_DEPTH {
        budget.last_error_location = Some(location.pointer());
        return Err(format!(
            "transform evaluation exceeded maximum recursion depth ({MAX_TRANSFORM_DEPTH})"
        ));
    }

    let value = match expr {
        TransformExprV52::SourceAttr { source_id, field } => {
            if !ctx.declared_sources.contains(source_id) {
                return Ok(String::new());
            }
            bounded_copy(
                ctx.matched_records
                    .get(source_id)
                    .and_then(|rec| rec.attributes.get(field))
                    .map_or("", String::as_str),
            )
        }
        TransformExprV52::Literal { value } => bounded_copy(value),
        TransformExprV52::RefTransform { name } => {
            if !visiting.insert(name.clone()) {
                budget.last_error_location =
                    Some(location.with_child_pointer(PointerSegment::Key("name")));
                return Err(format!("cyclic transform reference detected at '{name}'"));
            }
            let Some(target) = ctx.transforms.transforms.get(name) else {
                budget.last_error_location =
                    Some(location.with_child_pointer(PointerSegment::Key("name")));
                visiting.remove(name);
                return Err(format!("unknown transform reference '{name}'"));
            };
            let transforms_root = EvalLocation::root("config/transforms.json", "");
            let transforms_location = transforms_root.child(PointerSegment::Key("transforms"));
            let result = eval_child(
                target,
                ctx,
                depth,
                visiting,
                budget,
                &transforms_location,
                PointerSegment::Key(name),
            );
            visiting.remove(name);
            result
        }
        TransformExprV52::FirstValid {
            values,
            ignore_blank,
        } => {
            let mut chosen = String::new();
            for (index, candidate) in values.iter().enumerate() {
                let evaluated = eval_child(
                    candidate,
                    ctx,
                    depth,
                    visiting,
                    budget,
                    location,
                    PointerSegment::KeyIndex("values", index),
                )?;
                let valid = if *ignore_blank {
                    !evaluated.trim().is_empty()
                } else {
                    !evaluated.is_empty()
                };
                if valid {
                    chosen = evaluated;
                    break;
                }
            }
            Ok(chosen)
        }
        TransformExprV52::Lookup {
            input,
            table,
            default_value,
        } => {
            let key = eval_child(
                input,
                ctx,
                depth,
                visiting,
                budget,
                location,
                PointerSegment::Key("input"),
            )?;
            if let Some(mapped) = table.get(&key) {
                bounded_copy(mapped)
            } else if let Some(default_expr) = default_value {
                eval_child(
                    default_expr,
                    ctx,
                    depth,
                    visiting,
                    budget,
                    location,
                    PointerSegment::Key("default_value"),
                )
            } else {
                Ok(String::new())
            }
        }
        TransformExprV52::Concat { parts } => {
            let mut out = String::new();
            for (index, part) in parts.iter().enumerate() {
                append(
                    &mut out,
                    &eval_child(
                        part,
                        ctx,
                        depth,
                        visiting,
                        budget,
                        location,
                        PointerSegment::KeyIndex("parts", index),
                    )?,
                )?;
            }
            Ok(out)
        }
        TransformExprV52::Trim { input } => Ok(eval_child(
            input,
            ctx,
            depth,
            visiting,
            budget,
            location,
            PointerSegment::Key("input"),
        )?
        .trim()
        .to_owned()),
        TransformExprV52::Lower { input } => Ok(eval_child(
            input,
            ctx,
            depth,
            visiting,
            budget,
            location,
            PointerSegment::Key("input"),
        )?
        .to_lowercase()),
        TransformExprV52::Upper { input } => Ok(eval_child(
            input,
            ctx,
            depth,
            visiting,
            budget,
            location,
            PointerSegment::Key("input"),
        )?
        .to_uppercase()),
        TransformExprV52::StripDiacritics { input } => {
            let raw = eval_child(
                input,
                ctx,
                depth,
                visiting,
                budget,
                location,
                PointerSegment::Key("input"),
            )?;
            Ok(strip_diacritics(&raw))
        }
        TransformExprV52::RegexReplace {
            input,
            pattern,
            replacement,
        } => {
            let raw = eval_child(
                input,
                ctx,
                depth,
                visiting,
                budget,
                location,
                PointerSegment::Key("input"),
            )?;
            bounded_replace(&raw, pattern, replacement, budget)
        }
        TransformExprV52::Substring {
            input,
            start,
            max_len,
        } => {
            let raw = eval_child(
                input,
                ctx,
                depth,
                visiting,
                budget,
                location,
                PointerSegment::Key("input"),
            )?;
            Ok(raw.chars().skip(*start).take(*max_len).collect())
        }
        TransformExprV52::IsoCountry2 { input } => {
            let raw = eval_child(
                input,
                ctx,
                depth,
                visiting,
                budget,
                location,
                PointerSegment::Key("input"),
            )?;
            Ok(iso_country_2(&raw))
        }
        TransformExprV52::Conditional {
            left,
            cmp,
            right,
            if_true,
            if_false,
        } => {
            let left_val = eval_child(
                left,
                ctx,
                depth,
                visiting,
                budget,
                location,
                PointerSegment::Key("left"),
            )?;
            let right_val = match right {
                Some(r) => Some(eval_child(
                    r,
                    ctx,
                    depth,
                    visiting,
                    budget,
                    location,
                    PointerSegment::Key("right"),
                )?),
                None => None,
            };
            let matched = eval_compare_op(&left_val, *cmp, right_val.as_deref());
            if matched {
                eval_child(
                    if_true,
                    ctx,
                    depth,
                    visiting,
                    budget,
                    location,
                    PointerSegment::Key("if_true"),
                )
            } else {
                eval_child(
                    if_false,
                    ctx,
                    depth,
                    visiting,
                    budget,
                    location,
                    PointerSegment::Key("if_false"),
                )
            }
        }
        TransformExprV52::DateCompare {
            left,
            cmp,
            right,
            if_true,
            if_false,
        } => {
            let left_val = eval_child(
                left,
                ctx,
                depth,
                visiting,
                budget,
                location,
                PointerSegment::Key("left"),
            )?;
            let right_val = eval_child(
                right,
                ctx,
                depth,
                visiting,
                budget,
                location,
                PointerSegment::Key("right"),
            )?;
            let matched = eval_date_compare(&left_val, *cmp, &right_val, ctx.cutover_date);
            if matched {
                eval_child(
                    if_true,
                    ctx,
                    depth,
                    visiting,
                    budget,
                    location,
                    PointerSegment::Key("if_true"),
                )
            } else {
                eval_child(
                    if_false,
                    ctx,
                    depth,
                    visiting,
                    budget,
                    location,
                    PointerSegment::Key("if_false"),
                )
            }
        }
    }?;
    check_limit(value.len(), MAX_STRING_BYTES)?;
    budget.total_bytes += value.len();
    check_limit(budget.total_bytes, MAX_TOTAL_BYTES)?;
    #[cfg(test)]
    {
        budget.peak_string = budget.peak_string.max(value.len());
    }
    Ok(value)
}

/// Stream matches using the regex crate's capture interpolation grammar. Bound even
/// a single match's expansion before copying a referenced capture into the output.
fn bounded_replace(
    raw: &str,
    pattern: &str,
    replacement: &str,
    budget: &mut TransformBudget,
) -> Result<String, String> {
    let largest = raw.len().max(pattern.len()).max(replacement.len());
    check_limit(largest, MAX_REGEX_BYTES)?;
    #[cfg(test)]
    {
        budget.peak_regex = budget.peak_regex.max(largest);
    }
    budget.charge_regex(65_536 + pattern.len())?;
    let re = Regex::new(pattern)
        .map_err(|err| format!("invalid regex_replace pattern '{pattern}': {err}"))?;
    let names: std::collections::HashMap<_, _> = re
        .capture_names()
        .enumerate()
        .filter_map(|(index, name)| name.map(|name| (name, index)))
        .collect();
    // Tokenize once with the regex library's own interpolation grammar. All writes
    // to the match output, including named captures and literal runs, use append.
    enum Part {
        Literal(String),
        Capture(usize),
    }
    let mut parts = Vec::new();
    let mut tail = String::new();
    regex_automata::util::interpolate::string(
        replacement,
        |index, literal| {
            if !literal.is_empty() {
                parts.push(Part::Literal(std::mem::take(literal)));
            }
            parts.push(Part::Capture(index));
        },
        |name| names.get(name).copied(),
        &mut tail,
    );
    if !tail.is_empty() {
        parts.push(Part::Literal(tail));
    }
    let mut out = String::new();
    let mut end = 0;
    let mut matches = re.captures_iter(raw);
    loop {
        budget.charge_regex((raw.len() - end + 1) * (pattern.len() + 1))?;
        let Some(captures) = matches.next() else {
            break;
        };
        let matched = captures
            .get(0)
            .expect("a capture always contains the full match");
        append(&mut out, &raw[end..matched.start()])?;
        budget.charge_regex(replacement.len() + 1)?;
        for part in &parts {
            match part {
                Part::Literal(text) => append(&mut out, text)?,
                Part::Capture(index) => {
                    if let Some(group) = captures.get(*index) {
                        append(&mut out, group.as_str())?;
                    }
                }
            }
        }
        end = matched.end();
    }
    append(&mut out, &raw[end..])?;
    Ok(out)
}

/// Evaluate a `CompareOpV52` predicate between `left` and optional `right`.
#[must_use]
pub fn eval_compare_op(left: &str, cmp: CompareOpV52, right: Option<&str>) -> bool {
    let rhs = right.unwrap_or("");
    match cmp {
        CompareOpV52::Eq => left == rhs,
        CompareOpV52::Ne => left != rhs,
        CompareOpV52::Contains => !rhs.is_empty() && left.contains(rhs),
        CompareOpV52::StartsWith => left.starts_with(rhs),
        CompareOpV52::EndsWith => left.ends_with(rhs),
        CompareOpV52::InList => !rhs.is_empty() && rhs.split('|').any(|item| left == item),
        CompareOpV52::IsEmpty => left.trim().is_empty(),
        CompareOpV52::IsNotEmpty => !left.trim().is_empty(),
    }
}

/// Evaluate a `DateCompareOpV52` predicate (`"CUTOVER_DATE"` resolves to `cutover_date`).
/// Returns `false` if either side is empty after trimming.
#[must_use]
pub fn eval_date_compare(
    left_raw: &str,
    cmp: DateCompareOpV52,
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
            DateCompareOpV52::Lt => l_epoch < r_epoch,
            DateCompareOpV52::Le => l_epoch <= r_epoch,
            DateCompareOpV52::Gt => l_epoch > r_epoch,
            DateCompareOpV52::Ge => l_epoch >= r_epoch,
            DateCompareOpV52::Eq => l_epoch == r_epoch,
        };
    }

    match cmp {
        DateCompareOpV52::Lt => left < right,
        DateCompareOpV52::Le => left <= right,
        DateCompareOpV52::Gt => left > right,
        DateCompareOpV52::Ge => left >= right,
        DateCompareOpV52::Eq => left == right,
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
    Some((days_from_civil(i128::from(year), i128::from(month), i128::from(day)) * 86_400) as i64)
}

/// Howard Hinnant's civil-from-days inverse: days since 1970-01-01.
fn days_from_civil(year: i128, month: i128, day: i128) -> i128 {
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

/// Deterministic snake_case representation of `AffiliationTypeV52`.
#[must_use]
pub const fn affiliation_to_str(aff: &AffiliationTypeV52) -> &'static str {
    match aff {
        AffiliationTypeV52::Fte => "fte",
        AffiliationTypeV52::PartTimeStaff => "part_time_staff",
        AffiliationTypeV52::FacultyFullTime => "faculty_full_time",
        AffiliationTypeV52::AdjunctFaculty => "adjunct_faculty",
        AffiliationTypeV52::SeasonalWorker => "seasonal_worker",
        AffiliationTypeV52::StudentWorker => "student_worker",
        AffiliationTypeV52::Student => "student",
        AffiliationTypeV52::ClinicalProvider => "clinical_provider",
        AffiliationTypeV52::ClinicalResident => "clinical_resident",
        AffiliationTypeV52::ContingentWorker => "contingent_worker",
        AffiliationTypeV52::Vendor => "vendor",
        AffiliationTypeV52::Affiliate => "affiliate",
        AffiliationTypeV52::B2bGuest => "b2b_guest",
    }
}

/// Parse a snake_case `AffiliationTypeV52` token.
#[must_use]
pub fn parse_affiliation(token: &str) -> Option<AffiliationTypeV52> {
    match token.trim() {
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
        "affiliate" => Some(AffiliationTypeV52::Affiliate),
        "b2b_guest" => Some(AffiliationTypeV52::B2bGuest),
        _ => None,
    }
}

/// Deterministic snake_case representation of `LifecycleStateIdV52`.
#[must_use]
pub const fn lifecycle_state_to_str(state: LifecycleStateIdV52) -> &'static str {
    match state {
        LifecycleStateIdV52::LegalHold => "legal_hold",
        LifecycleStateIdV52::Terminated => "terminated",
        LifecycleStateIdV52::GardenLeave => "garden_leave",
        LifecycleStateIdV52::SuspendedCredential => "suspended_credential",
        LifecycleStateIdV52::ContingentExpiredGrace => "contingent_expired_grace",
        LifecycleStateIdV52::SeasonalFurlough => "seasonal_furlough",
        LifecycleStateIdV52::LeaveOfAbsence => "leave_of_absence",
        LifecycleStateIdV52::PreHire => "pre_hire",
        LifecycleStateIdV52::Active => "active",
        LifecycleStateIdV52::InactiveOrphan => "inactive_orphan",
    }
}

/// Deterministic snake_case representation of `GroupDispositionV52`.
#[must_use]
pub const fn group_disposition_to_str(disp: GroupDispositionV52) -> &'static str {
    match disp {
        GroupDispositionV52::ApplicationEntitlement => "application_entitlement",
        GroupDispositionV52::BirthrightBundleCandidate => "birthright_bundle_candidate",
        GroupDispositionV52::PrivilegedAdminRequestOnly => "privileged_admin_request_only",
        GroupDispositionV52::OrphanedDeprecatedDoNotAssign => "orphaned_deprecated_do_not_assign",
        GroupDispositionV52::ServiceAccountOnlyDoNotAssign => "service_account_only_do_not_assign",
    }
}

#[cfg(test)]
mod date_overflow_tests {
    use super::parse_date_or_epoch;

    #[test]
    fn date_conversion_preserves_wrapping_results_without_panicking() {
        assert_eq!(parse_date_or_epoch("1970-01-01"), Some(0));
        assert_eq!(parse_date_or_epoch("1969-12-31"), Some(-86_400));
        assert_eq!(
            parse_date_or_epoch("9223372036854775807-01-01"),
            Some(-62_198_793_216)
        );
        assert_eq!(parse_date_or_epoch("-9223372036854775808-01-01"), None);
    }
}

#[cfg(test)]
mod budget_tests {
    use super::*;
    use ib_v52_schemas::{EpisodeV52, SealedEpisodeOracleV52, V52Suite};
    use std::fs::File;
    use std::io::{BufRead, BufReader};
    use std::path::Path;

    #[test]
    fn bounded_replacements_preserve_regex_interpolation() {
        let raw = "éAB ba";
        for (pattern, replacement) in [
            ("(?P<x>A)(B)?", "${x}.$2/$0 $$ ${missing} $99"),
            ("", "|"),
            ("(?P<absent>X)?b", "<$absent>"),
            ("z", "$0"),
        ] {
            let expected = Regex::new(pattern).unwrap().replace_all(raw, replacement);
            assert_eq!(
                bounded_replace(raw, pattern, replacement, &mut TransformBudget::default())
                    .unwrap(),
                expected
            );
        }
    }

    #[test]
    fn expanded_references_share_node_and_output_allowances() {
        let mut transforms = TransformsConfigV52::default();
        transforms.transforms.insert(
            "constant".into(),
            TransformExprV52::Literal { value: "ab".into() },
        );
        let ctx = TransformEvalContext {
            transforms: &transforms,
            declared_sources: &HashSet::new(),
            matched_records: &IndexMap::new(),
            cutover_date: "",
        };
        let expr = TransformExprV52::RefTransform {
            name: "constant".into(),
        };
        let mut nodes = TransformBudget {
            nodes: MAX_NODES - 2,
            ..Default::default()
        };
        assert_eq!(nodes.evaluate(&expr, &ctx).unwrap(), "ab");
        assert_eq!(
            nodes.evaluate(&expr, &ctx).unwrap_err(),
            TRANSFORM_BUDGET_EXCEEDED
        );
        let mut bytes = TransformBudget {
            total_bytes: MAX_TOTAL_BYTES - 3,
            ..Default::default()
        };
        assert_eq!(
            bytes.evaluate(&expr, &ctx).unwrap_err(),
            TRANSFORM_BUDGET_EXCEEDED
        );
        let mut regex_budget = TransformBudget {
            regex_work: MAX_REGEX_WORK,
            ..Default::default()
        };
        assert_eq!(
            bounded_replace("a", "a", "x", &mut regex_budget).unwrap_err(),
            TRANSFORM_BUDGET_EXCEEDED
        );
    }

    #[test]
    fn capture_output_is_bounded_during_expansion() {
        let raw = "x".repeat(MAX_REGEX_BYTES);
        assert_eq!(
            bounded_replace(
                &raw,
                "(.+)",
                &"$1".repeat(8),
                &mut TransformBudget::default()
            )
            .unwrap()
            .len(),
            MAX_STRING_BYTES
        );
        assert_eq!(
            bounded_replace(
                &raw,
                "(.+)",
                &"$1".repeat(9),
                &mut TransformBudget::default()
            )
            .unwrap_err(),
            TRANSFORM_BUDGET_EXCEEDED
        );
    }

    // Aggregate resource use only. No episode contents or per-episode results are emitted.
    #[test]
    fn gold_transform_budget_headroom() {
        for suite in V52Suite::PUBLIC {
            let dir = Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../datasets/v52")
                .join(suite.as_str());
            let lines = |name| BufReader::new(File::open(dir.join(name)).unwrap()).lines();
            let mut maxima = [0; 5];
            let mut count = 0;
            for (episode, oracle) in lines("episodes.jsonl").zip(lines("oracles.jsonl")) {
                let episode: EpisodeV52 = serde_json::from_str(&episode.unwrap()).unwrap();
                let oracle: SealedEpisodeOracleV52 =
                    serde_json::from_str(&oracle.unwrap()).unwrap();
                assert_eq!(episode.episode_id, oracle.episode_id);
                let mut budget = TransformBudget::default();
                crate::IgaEngineV52::evaluate_world(
                    &episode.world,
                    &oracle.gold_config,
                    &episode.cutover_date,
                    &mut budget,
                )
                .unwrap();
                for (peak, used) in maxima.iter_mut().zip([
                    budget.nodes,
                    budget.peak_string,
                    budget.peak_regex,
                    budget.total_bytes,
                    budget.regex_work,
                ]) {
                    *peak = (*peak).max(used);
                }
                count += 1;
            }
            assert_eq!(count, suite.episode_count());
            eprintln!(
                "{suite} {count} gold passes: nodes, string bytes, regex operand bytes, total bytes, regex work = {maxima:?}"
            );
            for (peak, limit) in maxima.into_iter().zip([
                MAX_NODES,
                MAX_STRING_BYTES,
                MAX_REGEX_BYTES,
                MAX_TOTAL_BYTES,
                MAX_REGEX_WORK,
            ]) {
                assert!(peak * 100 <= limit);
            }
        }
    }
}
