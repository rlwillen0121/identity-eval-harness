//! ISO `YYYY-MM-DD` date arithmetic relative to the cutover date.

pub(super) fn prev_day_iso(cutover_date: &str, sub_days: u32) -> String {
    let parts: Vec<&str> = cutover_date.split('-').collect();
    if parts.len() == 3
        && let (Ok(y), Ok(m), Ok(d)) = (
            parts[0].parse::<u32>(),
            parts[1].parse::<u32>(),
            parts[2].parse::<u32>(),
        )
        && d > sub_days
    {
        return format!("{y:04}-{m:02}-{:02}", d - sub_days);
    }
    "2026-11-05".to_owned()
}

/// Shift an ISO `YYYY-MM-DD` date by `delta_days` using exact proleptic-Gregorian arithmetic.
/// Used by the Appendix C mechanisms; the older `prev_day_iso`/`next_day_iso` helpers stay unchanged
/// so carried-forward episodes keep their bytes.
pub(crate) fn shift_iso_date(date: &str, delta_days: i64) -> String {
    let mut parts = date.split('-').map(str::parse::<i64>);
    let (Some(Ok(y)), Some(Ok(m)), Some(Ok(d))) = (parts.next(), parts.next(), parts.next()) else {
        return date.to_owned();
    };
    // Howard Hinnant's days_from_civil / civil_from_days.
    let yy = if m <= 2 { y - 1 } else { y };
    let era = yy.div_euclid(400);
    let yoe = yy - era * 400;
    let mp = if m > 2 { m - 3 } else { m + 9 };
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let z = era * 146_097 + doe - 719_468 + delta_days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02}")
}

pub(super) fn next_day_iso(cutover_date: &str, add_days: u32) -> String {
    let parts: Vec<&str> = cutover_date.split('-').collect();
    if parts.len() == 3
        && let (Ok(y), Ok(m), Ok(d)) = (
            parts[0].parse::<u32>(),
            parts[1].parse::<u32>(),
            parts[2].parse::<u32>(),
        )
    {
        let new_d = d + add_days;
        if new_d <= 28 {
            return format!("{y:04}-{m:02}-{new_d:02}");
        }
        return format!("{y:04}-{:02}-{:02}", m + 1, new_d - 28);
    }
    "2026-11-25".to_owned()
}
