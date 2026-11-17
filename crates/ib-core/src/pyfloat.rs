//! CPython-compatible float formatting.
//!
//! `repr(float)` in CPython (and therefore `json.dumps(float)`) prints the shortest
//! digit string that round-trips, switching to exponent notation when the decimal
//! exponent is `< -4` or `>= 16`. Rust's `{:e}` formatting yields the same shortest
//! digits, so we only re-layout them.

/// Decompose a finite, non-zero `f64` into its shortest round-trip decimal digits and
/// the CPython `decpt` (value = 0.DIGITS x 10^decpt).
fn shortest_digits(x: f64) -> (String, i32) {
    let s = format!("{:e}", x.abs());
    let (mantissa, exp) = s.split_once('e').expect("LowerExp always has an exponent");
    let exp: i32 = exp.parse().expect("LowerExp exponent is an integer");
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    // Strip trailing zeros (LowerExp never emits them for the shortest repr, but be safe).
    let digits = digits.trim_end_matches('0');
    let digits = if digits.is_empty() { "0" } else { digits };
    (digits.to_string(), exp + 1)
}

/// `repr(x)` for a Python float (`'inf'`, `'nan'`, `'-0.0'`, `'1e-05'`, `'1.0'`, ...).
#[must_use]
pub fn repr(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_string();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".to_string() } else { "-inf".to_string() };
    }
    let sign = if x.is_sign_negative() { "-" } else { "" };
    if x == 0.0 {
        return format!("{sign}0.0");
    }
    let (digits, decpt) = shortest_digits(x);
    let n = i32::try_from(digits.len()).expect("digit count fits i32");
    let body = if decpt <= -4 || decpt > 16 {
        let exp = decpt - 1;
        let mut mant = digits[..1].to_string();
        if n > 1 {
            mant.push('.');
            mant.push_str(&digits[1..]);
        }
        let esign = if exp < 0 { '-' } else { '+' };
        format!("{mant}e{esign}{:02}", exp.abs())
    } else if decpt <= 0 {
        format!("0.{}{}", "0".repeat(usize::try_from(-decpt).unwrap_or(0)), digits)
    } else if decpt < n {
        let split = usize::try_from(decpt).unwrap_or(0);
        format!("{}.{}", &digits[..split], &digits[split..])
    } else {
        format!(
            "{}{}.0",
            digits,
            "0".repeat(usize::try_from(decpt - n).unwrap_or(0))
        )
    };
    format!("{sign}{body}")
}

/// `str(x)` for a Python float (identical to `repr` since Python 3.2).
#[must_use]
pub fn str(x: f64) -> String {
    repr(x)
}

/// Python `format(x, f".{precision}f")` / `f"{x:.{precision}f}"`.
///
/// Both CPython and Rust round the exact binary value correctly (ties-to-even on exact
/// decimal ties), so the standard formatter matches; we only normalise `nan`/`inf`.
#[must_use]
pub fn format_fixed(x: f64, precision: usize) -> String {
    if x.is_nan() {
        return "nan".to_string();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".to_string() } else { "-inf".to_string() };
    }
    format!("{x:.precision$}")
}

/// Python `round(x, ndigits)` for floats (correctly rounded, ties-to-even on the exact
/// decimal value), implemented via CPython's string round-trip strategy.
#[must_use]
pub fn round_ndigits(x: f64, ndigits: i32) -> f64 {
    if !x.is_finite() || x == 0.0 {
        return x;
    }
    if ndigits > 22 {
        return x;
    }
    if ndigits >= 0 {
        // CPython: _Py_dg_dtoa(x, 3, ndigits, ...) then strtod. Rust's fixed formatting is
        // correctly rounded with ties-to-even on the exact binary value, which is the same.
        let s = format!("{:.*}", usize::try_from(ndigits).unwrap_or(0), x);
        s.parse::<f64>().unwrap_or(x)
    } else {
        let pow = 10f64.powi(-ndigits);
        let y = x / pow;
        let r = y.round_ties_even();
        let z = r * pow;
        if z.is_finite() { z } else { x }
    }
}

/// Python built-in `round(x)` with no ndigits: banker's rounding to an integer.
#[must_use]
pub fn round_half_even(x: f64) -> f64 {
    x.round_ties_even()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repr_matches_cpython_examples() {
        let cases: &[(f64, &str)] = &[
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (-1.5, "-1.5"),
            (0.1, "0.1"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1e16, "1e+16"),
            (1e15, "1000000000000000.0"),
            (123_456_789_012_345_680.0, "1.2345678901234568e+17"),
            (2.5e-300, "2.5e-300"),
            (1.0 / 3.0, "0.3333333333333333"),
            (100.0, "100.0"),
            (0.5, "0.5"),
            (1e22, "1e+22"),
            (f64::MAX, "1.7976931348623157e+308"),
            (5e-324, "5e-324"),
        ];
        for (x, want) in cases {
            assert_eq!(repr(*x), *want, "repr({x:?})");
        }
    }

    #[test]
    fn round_matches_cpython_examples() {
        assert_eq!(round_ndigits(2.675, 2), 2.67);
        assert_eq!(round_ndigits(0.125, 2), 0.12);
        assert_eq!(round_ndigits(0.375, 2), 0.38);
        assert_eq!(round_half_even(2.5), 2.0);
        assert_eq!(round_half_even(3.5), 4.0);
    }
}
