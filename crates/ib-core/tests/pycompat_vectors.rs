//! CPython golden vectors (see `fixtures/gen_pycompat_vectors.py`).

use ib_core::pyfloat;
use ib_core::pyjson::escape_str;
use ib_core::pyrandom::PyRandom;
use serde_json::Value;

fn vectors() -> Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/pycompat_vectors.json"
    );
    serde_json::from_slice(&std::fs::read(path).expect("fixture")).expect("json")
}

#[test]
fn float_repr_fixed_and_round_match_cpython() {
    let v = vectors();
    let mut checked = 0;
    for case in v["floats"].as_array().unwrap() {
        let bits = case["bits"].as_u64().unwrap();
        let f = f64::from_bits(bits);
        assert_eq!(
            pyfloat::repr(f),
            case["repr"].as_str().unwrap(),
            "repr bits={bits:#x}"
        );
        if let Some(want) = case["fixed2"].as_str() {
            assert_eq!(pyfloat::format_fixed(f, 2), want, "fixed2 {f:?}");
        }
        if let Some(want) = case["fixed4"].as_str() {
            assert_eq!(pyfloat::format_fixed(f, 4), want, "fixed4 {f:?}");
        }
        if let Some(want) = case["round3"].as_str() {
            assert_eq!(
                pyfloat::repr(pyfloat::round_ndigits(f, 3)),
                want,
                "round3 {f:?}"
            );
        }
        checked += 1;
    }
    assert!(checked > 5000);
}

fn seeded(case: &Value) -> PyRandom {
    let seed = case["seed"].as_str().unwrap();
    if case["seed_kind"] == "str" {
        PyRandom::from_str_seed(seed)
    } else {
        PyRandom::from_i128(seed.parse::<i128>().unwrap())
    }
}

#[test]
fn random_streams_match_cpython() {
    let v = vectors();
    for case in v["rng"].as_array().unwrap() {
        let label = case["seed"].to_string();
        let mut r = seeded(case);
        for want in case["random"].as_array().unwrap() {
            assert_eq!(
                pyfloat::repr(r.random()),
                want.as_str().unwrap(),
                "random {label}"
            );
        }
        for (k, want) in [1u32, 7, 32, 33, 64, 100]
            .iter()
            .zip(case["getrandbits"].as_array().unwrap())
        {
            assert_eq!(
                r.getrandbits(*k).to_string(),
                want.as_str().unwrap(),
                "getrandbits({k}) {label}"
            );
        }
        for (n, want) in [1u128, 2, 3, 10, 1000, (1u128 << 40) + 3]
            .iter()
            .zip(case["randrange"].as_array().unwrap())
        {
            assert_eq!(
                r.randbelow(*n).to_string(),
                want.as_str().unwrap(),
                "randrange({n}) {label}"
            );
        }
        let mut x: Vec<u64> = (0..37).collect();
        r.shuffle(&mut x);
        let want: Vec<u64> = case["shuffle37"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap())
            .collect();
        assert_eq!(x, want, "shuffle {label}");
        let pop: Vec<u64> = (0..30).collect();
        let want: Vec<u64> = case["sample7of30"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap())
            .collect();
        assert_eq!(r.sample(&pop, 7), want, "sample small {label}");
        let pop: Vec<u64> = (0..5000).collect();
        let want: Vec<u64> = case["sample9of5000"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap())
            .collect();
        assert_eq!(r.sample(&pop, 9), want, "sample big {label}");
        let pop: Vec<u64> = (0..11).collect();
        assert_eq!(
            *r.choice(&pop).unwrap(),
            case["choice11"].as_u64().unwrap(),
            "choice {label}"
        );
        assert_eq!(
            pyfloat::repr(r.uniform(-3.0, 9.0)),
            case["uniform"].as_str().unwrap(),
            "uniform {label}"
        );
        let pop: Vec<u64> = (0..13).collect();
        let want: Vec<u64> = case["choices6of13"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap())
            .collect();
        assert_eq!(r.choices_unweighted(&pop, 6), want, "choices {label}");
    }
}

#[test]
fn string_escaping_matches_cpython() {
    let v = vectors();
    for case in v["strings"].as_array().unwrap() {
        let s = case["s"].as_str().unwrap();
        assert_eq!(escape_str(s, true), case["ascii"].as_str().unwrap());
        assert_eq!(escape_str(s, false), case["noascii"].as_str().unwrap());
    }
}
