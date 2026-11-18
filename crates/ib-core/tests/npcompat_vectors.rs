//! NumPy 2.5.4 golden vectors (see `fixtures/gen_npcompat_vectors.py`).

use ib_core::hashing::sha256_hex;
use ib_core::npcompat::{
    Generator, NpError, Pcg64, SeedSequence, dot, mean_axis1, mean_pairwise, percentile_linear,
    percentiles_linear,
};
use serde_json::Value;

fn vectors() -> Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/npcompat_vectors.json"
    );
    serde_json::from_slice(&std::fs::read(path).expect("fixture")).expect("json")
}

fn bits_slice(arr: &[Value]) -> Vec<f64> {
    arr.iter()
        .map(|v| f64::from_bits(v.as_u64().unwrap()))
        .collect()
}

fn assert_f64_bits(got: f64, want_bits: u64, ctx: &str) {
    let want = f64::from_bits(want_bits);
    if want.is_nan() {
        assert!(
            got.is_nan(),
            "{ctx}: expected NaN, got {got:?} ({:#018x})",
            got.to_bits()
        );
    } else {
        assert_eq!(
            got.to_bits(),
            want_bits,
            "{ctx}: got {got:?} ({:#018x}) != want {want:?} ({want_bits:#018x})",
            got.to_bits()
        );
    }
}

#[test]
fn seed_sequence_matches_numpy() {
    let v = vectors();
    for case in v["seed_sequence"].as_array().unwrap() {
        let kind = case["kind"].as_str().unwrap();
        let seed_str = case["seed"].as_str().unwrap();
        let ss = match kind {
            "u64" => SeedSequence::from_u64(seed_str.parse::<u64>().unwrap()),
            "u128" => SeedSequence::from_u128(seed_str.parse::<u128>().unwrap()),
            "u32_words" => {
                let words: Vec<u32> = seed_str
                    .split(',')
                    .map(|s| s.parse::<u32>().unwrap())
                    .collect();
                SeedSequence::from_u32_words(&words, 4)
            }
            other => panic!("unexpected seed kind: {other}"),
        };
        let want_pool: Vec<u32> = case["pool"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| u32::try_from(x.as_u64().unwrap()).unwrap())
            .collect();
        assert_eq!(ss.pool(), want_pool.as_slice(), "pool {kind}:{seed_str}");

        let want_u32: Vec<u32> = case["u32_8"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| u32::try_from(x.as_u64().unwrap()).unwrap())
            .collect();
        assert_eq!(
            ss.generate_state_u32(8),
            want_u32,
            "u32_8 {kind}:{seed_str}"
        );

        let want_u64: Vec<u64> = case["u64_4"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap().parse::<u64>().unwrap())
            .collect();
        assert_eq!(
            ss.generate_state_u64(4),
            want_u64,
            "u64_4 {kind}:{seed_str}"
        );
    }
}

#[test]
fn pcg64_streams_match_numpy() {
    let v = vectors();
    for case in v["pcg64"].as_array().unwrap() {
        let seed = case["seed"].as_str().unwrap().parse::<u64>().unwrap();
        let mut pcg = Pcg64::new(seed);
        let (st, inc, has_u32, u_val) = pcg.state_tuple();
        assert_eq!(
            st,
            case["init_state"]
                .as_str()
                .unwrap()
                .parse::<u128>()
                .unwrap()
        );
        assert_eq!(
            inc,
            case["init_inc"].as_str().unwrap().parse::<u128>().unwrap()
        );
        assert!(!has_u32);
        assert_eq!(u_val, 0);

        let want_u64: Vec<u64> = case["u64_8"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap().parse::<u64>().unwrap())
            .collect();
        let got_u64: Vec<u64> = (0..8).map(|_| pcg.next_u64()).collect();
        assert_eq!(got_u64, want_u64, "u64_8 seed={seed}");

        let mut pcg_u32 = Pcg64::new(seed);
        let want_u32: Vec<u32> = case["u32_9"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| u32::try_from(x.as_u64().unwrap()).unwrap())
            .collect();
        let got_u32: Vec<u32> = (0..9).map(|_| pcg_u32.next_u32()).collect();
        assert_eq!(got_u32, want_u32, "u32_9 seed={seed}");
        let (st32, _, has32, val32) = pcg_u32.state_tuple();
        assert_eq!(
            st32,
            case["after_u32_state"]
                .as_str()
                .unwrap()
                .parse::<u128>()
                .unwrap()
        );
        assert_eq!(has32, case["after_u32_has"].as_bool().unwrap());
        assert_eq!(
            val32,
            u32::try_from(case["after_u32_val"].as_u64().unwrap()).unwrap()
        );

        let mut pcg_f64 = Pcg64::new(seed);
        for (idx, want) in case["f64_6_bits"].as_array().unwrap().iter().enumerate() {
            assert_f64_bits(
                pcg_f64.next_f64(),
                want.as_u64().unwrap(),
                &format!("f64 seed={seed}[{idx}]"),
            );
        }
    }
}

#[test]
fn generator_integers_match_numpy() {
    let v = vectors();
    let ints = &v["integers"];

    let mut rng = Generator::new(20261111);
    let mat = rng.integers_usize_2d(0, 12, 10_000, 12).unwrap();
    assert_eq!(mat.len(), 120_000);

    let mut raw_bytes = Vec::with_capacity(mat.len() * 8);
    for &idx in &mat {
        raw_bytes.extend_from_slice(&(idx as i64).to_le_bytes());
    }
    assert_eq!(
        sha256_hex(&raw_bytes),
        ints["bootstrap_matrix_sha256"].as_str().unwrap()
    );

    let head3: Vec<Vec<usize>> = ints["head3"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            row.as_array()
                .unwrap()
                .iter()
                .map(|x| usize::try_from(x.as_u64().unwrap()).unwrap())
                .collect()
        })
        .collect();
    for (r, want_row) in head3.iter().enumerate() {
        assert_eq!(
            &mat[r * 12..(r + 1) * 12],
            want_row.as_slice(),
            "head row {r}"
        );
    }

    let tail3: Vec<Vec<usize>> = ints["tail3"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            row.as_array()
                .unwrap()
                .iter()
                .map(|x| usize::try_from(x.as_u64().unwrap()).unwrap())
                .collect()
        })
        .collect();
    for (offset, want_row) in tail3.iter().enumerate() {
        let r = 10_000 - 3 + offset;
        assert_eq!(
            &mat[r * 12..(r + 1) * 12],
            want_row.as_slice(),
            "tail row {r}"
        );
    }

    for case in ints["wide"].as_array().unwrap() {
        let seed = case["seed"].as_str().unwrap().parse::<u64>().unwrap();
        let low = case["low"].as_str().unwrap().parse::<i64>().unwrap();
        let high = case["high"].as_str().unwrap().parse::<i64>().unwrap();
        let count = usize::try_from(case["count"].as_u64().unwrap()).unwrap();
        let want: Vec<i64> = case["values"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap().parse::<i64>().unwrap())
            .collect();
        let got = Generator::new(seed).integers_i64(low, high, count).unwrap();
        assert_eq!(got, want, "wide seed={seed} [{low}, {high})");
    }
}

#[test]
fn mean_pairwise_and_axis1_match_numpy_bits() {
    let v = vectors();
    let m = &v["mean"];
    let src_300 = bits_slice(m["src_300_bits"].as_array().unwrap());
    for (idx, want) in m["means_1_to_300_bits"]
        .as_array()
        .unwrap()
        .iter()
        .enumerate()
    {
        let n = idx + 1;
        assert_f64_bits(
            mean_pairwise(&src_300[..n]),
            want.as_u64().unwrap(),
            &format!("1d mean n={n}"),
        );
    }

    for (n_val, want_bits) in m["neg_zero_ns"]
        .as_array()
        .unwrap()
        .iter()
        .zip(m["neg_zero_means_bits"].as_array().unwrap())
    {
        let n = usize::try_from(n_val.as_u64().unwrap()).unwrap();
        let arr = vec![-0.0_f64; n];
        assert_f64_bits(
            mean_pairwise(&arr),
            want_bits.as_u64().unwrap(),
            &format!("-0.0 mean n={n}"),
        );
    }

    assert!(mean_pairwise(&[]).is_nan());

    let mat_100_129 = bits_slice(m["mat_100_129_bits"].as_array().unwrap());
    let got_100_129 = mean_axis1(&mat_100_129, 100, 129);
    for (row, (got, want)) in got_100_129
        .iter()
        .zip(m["means_100_129_bits"].as_array().unwrap())
        .enumerate()
    {
        assert_f64_bits(
            *got,
            want.as_u64().unwrap(),
            &format!("(100,129) row {row}"),
        );
    }

    let fam_12 = bits_slice(m["fam_12_bits"].as_array().unwrap());
    let sampled = Generator::new(20261111)
        .integers_usize_2d(0, 12, 10_000, 12)
        .unwrap();
    let sampled_vals: Vec<f64> = sampled.iter().map(|&idx| fam_12[idx]).collect();
    let draws = mean_axis1(&sampled_vals, 10_000, 12);
    let mut draw_bytes = Vec::with_capacity(draws.len() * 8);
    for &d in &draws {
        draw_bytes.extend_from_slice(&d.to_le_bytes());
    }
    assert_eq!(
        sha256_hex(&draw_bytes),
        m["draws_10000_sha256"].as_str().unwrap()
    );
    for (i, want) in m["draws_head5_bits"].as_array().unwrap().iter().enumerate() {
        assert_f64_bits(draws[i], want.as_u64().unwrap(), &format!("draws head {i}"));
    }
    for (i, want) in m["draws_tail5_bits"].as_array().unwrap().iter().enumerate() {
        assert_f64_bits(
            draws[10_000 - 5 + i],
            want.as_u64().unwrap(),
            &format!("draws tail {i}"),
        );
    }
}

#[test]
fn percentile_linear_matches_numpy_bits() {
    let v = vectors();
    for case in v["percentile"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let values = bits_slice(case["values_bits"].as_array().unwrap());
        let qs: Vec<f64> = case["qs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|q| q.as_f64().unwrap())
            .collect();
        let multi = percentiles_linear(&values, &qs).unwrap();
        for (idx, (got, want)) in multi
            .iter()
            .zip(case["multi_bits"].as_array().unwrap())
            .enumerate()
        {
            assert_f64_bits(
                *got,
                want.as_u64().unwrap(),
                &format!("{name} multi[{idx}]"),
            );
        }
        for (idx, (&q, want)) in qs
            .iter()
            .zip(case["single_bits"].as_array().unwrap())
            .enumerate()
        {
            let got = percentile_linear(&values, q).unwrap();
            assert_f64_bits(
                got,
                want.as_u64().unwrap(),
                &format!("{name} single[{idx}] q={q}"),
            );
        }
    }
}

#[test]
fn dot_matches_numpy_bits() {
    let v = vectors();
    for (idx, case) in v["dot"].as_array().unwrap().iter().enumerate() {
        let a = bits_slice(case["a_bits"].as_array().unwrap());
        let b = bits_slice(case["b_bits"].as_array().unwrap());
        let got = dot(&a, &b);
        assert_f64_bits(
            got,
            case["dot_bits"].as_u64().unwrap(),
            &format!("dot[{idx}]"),
        );
    }
}

#[test]
fn synthetic_family_cluster_bootstrap_matches_numpy_bits() {
    let v = vectors();
    let boot = &v["bootstrap"];
    let models_map = boot["models_bits"].as_object().unwrap();
    let mut model_names: Vec<String> = models_map.keys().cloned().collect();
    model_names.sort();

    let sampled = Generator::new(20261111)
        .integers_usize_2d(0, 12, 10_000, 12)
        .unwrap();

    let mut means = Vec::with_capacity(model_names.len());
    let mut draws_by_model = Vec::with_capacity(model_names.len());

    for name in &model_names {
        let vec = bits_slice(models_map[name].as_array().unwrap());
        let mean = mean_pairwise(&vec);
        let matrix: Vec<f64> = sampled.iter().map(|&idx| vec[idx]).collect();
        let draws = mean_axis1(&matrix, 10_000, 12);
        let ci = percentiles_linear(&draws, &[2.5, 97.5]).unwrap();

        let want = &boot["intervals"][name];
        assert_f64_bits(
            mean,
            want["mean_bits"].as_u64().unwrap(),
            &format!("{name}.mean"),
        );
        assert_f64_bits(
            ci[0],
            want["low_bits"].as_u64().unwrap(),
            &format!("{name}.low"),
        );
        assert_f64_bits(
            ci[1],
            want["high_bits"].as_u64().unwrap(),
            &format!("{name}.high"),
        );

        means.push(mean);
        draws_by_model.push(draws);
    }

    let mut pair_idx = 0;
    for i in 0..model_names.len() {
        for j in (i + 1)..model_names.len() {
            let want = &boot["paired"][pair_idx];
            assert_eq!(want["model_a"].as_str().unwrap(), model_names[i]);
            assert_eq!(want["model_b"].as_str().unwrap(), model_names[j]);

            let diff = means[i] - means[j];
            let diff_draws: Vec<f64> = draws_by_model[i]
                .iter()
                .zip(draws_by_model[j].iter())
                .map(|(a, b)| a - b)
                .collect();
            let ci = percentiles_linear(&diff_draws, &[2.5, 97.5]).unwrap();

            let label = format!("{}-{}", model_names[i], model_names[j]);
            assert_f64_bits(
                diff,
                want["diff_bits"].as_u64().unwrap(),
                &format!("{label}.diff"),
            );
            assert_f64_bits(
                ci[0],
                want["low_bits"].as_u64().unwrap(),
                &format!("{label}.low"),
            );
            assert_f64_bits(
                ci[1],
                want["high_bits"].as_u64().unwrap(),
                &format!("{label}.high"),
            );
            pair_idx += 1;
        }
    }
    assert_eq!(pair_idx, boot["paired"].as_array().unwrap().len());
}

#[test]
fn error_cases() {
    assert_eq!(percentile_linear(&[], 50.0), Err(NpError::EmptyInput));
    assert_eq!(percentiles_linear(&[], &[50.0]), Err(NpError::EmptyInput));
    assert_eq!(
        percentile_linear(&[1.0], -0.0001),
        Err(NpError::InvalidPercentile(-0.0001))
    );
    assert_eq!(
        percentile_linear(&[1.0], 100.0001),
        Err(NpError::InvalidPercentile(100.0001))
    );
    assert!(matches!(
        percentile_linear(&[1.0], f64::NAN),
        Err(NpError::InvalidPercentile(q)) if q.is_nan()
    ));

    let mut g = Generator::new(1);
    assert_eq!(
        g.integers_i64(5, 5, 1),
        Err(NpError::InvalidRange { low: 5, high: 5 })
    );
    assert_eq!(
        g.integers_i64(5, 4, 1),
        Err(NpError::InvalidRange { low: 5, high: 4 })
    );
    assert_eq!(
        g.integers_usize_2d(-1, 5, 2, 2),
        Err(NpError::InvalidRange { low: -1, high: 5 })
    );
}
