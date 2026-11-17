"""Regenerate NumPy 2.5.4 golden vectors for ib-core (run: uv run python crates/ib-core/tests/fixtures/gen_npcompat_vectors.py)."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import numpy as np

OUT = Path(__file__).with_name("npcompat_vectors.json")


def f64_bits(x: float) -> int:
    return struct.unpack("<Q", struct.pack("<d", float(x)))[0]


def seed_sequence_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    specs: list[tuple[str, object, str]] = [
        ("u64", 0, "0"),
        ("u64", 1, "1"),
        ("u64", 42, "42"),
        ("u64", 20261111, "20261111"),
        ("u64", (1 << 64) - 1, str((1 << 64) - 1)),
        ("u128", 1 << 64, str(1 << 64)),
        ("u128", (1 << 160) + 12345, str((1 << 160) + 12345)),
        ("u32_words", [0, 1], "0,1"),
        ("u32_words", [1, 2, 3, 4, 5], "1,2,3,4,5"),
    ]
    for kind, val, label in specs:
        # Note: (1 << 160) + 12345 exceeds u128; handle u128 case separately below.
        if kind == "u128" and int(val) >= (1 << 128):
            continue
        ss = np.random.SeedSequence(val)
        cases.append(
            {
                "kind": kind,
                "seed": label,
                "pool": [int(x) for x in ss.pool],
                "u32_8": [int(x) for x in ss.generate_state(8, np.uint32)],
                "u64_4": [str(int(x)) for x in ss.generate_state(4, np.uint64)],
            }
        )
    # Also test 6 words via u32_words matching (1 << 160) + 12345.
    big = (1 << 160) + 12345
    ss_big = np.random.SeedSequence(big)
    words_big = [(big >> (32 * i)) & 0xFFFFFFFF for i in range(6)]
    cases.append(
        {
            "kind": "u32_words",
            "seed": ",".join(str(w) for w in words_big),
            "pool": [int(x) for x in ss_big.pool],
            "u32_8": [int(x) for x in ss_big.generate_state(8, np.uint32)],
            "u64_4": [str(int(x)) for x in ss_big.generate_state(4, np.uint64)],
        }
    )
    return cases


def pcg64_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for seed in [0, 1, 42, 20261111, (1 << 64) - 1]:
        pcg = np.random.PCG64(seed)
        init_st = pcg.state["state"]
        u64s = [int(pcg.random_raw()) for _ in range(8)]

        pcg_u32 = np.random.PCG64(seed)
        gen_u32 = np.random.Generator(pcg_u32)
        # integers(0, 2**32, dtype=np.int64) draws buffered next_u32() directly.
        u32s = [int(x) for x in gen_u32.integers(0, 1 << 32, size=9, dtype=np.int64)]
        after_u32 = pcg_u32.state

        pcg_f64 = np.random.PCG64(seed)
        gen_f64 = np.random.Generator(pcg_f64)
        f64_bits_list = [f64_bits(x) for x in gen_f64.random(6)]

        cases.append(
            {
                "seed": str(seed),
                "init_state": str(init_st["state"]),
                "init_inc": str(init_st["inc"]),
                "u64_8": [str(x) for x in u64s],
                "u32_9": u32s,
                "after_u32_state": str(after_u32["state"]["state"]),
                "after_u32_has": bool(after_u32["has_uint32"]),
                "after_u32_val": int(after_u32["uinteger"]),
                "f64_6_bits": f64_bits_list,
            }
        )
    return cases


def integers_cases() -> dict[str, object]:
    gen = np.random.Generator(np.random.PCG64(20261111))
    mat = gen.integers(0, 12, size=(10000, 12), dtype=np.int64)
    mat_sha256 = hashlib.sha256(mat.astype("<i8").tobytes()).hexdigest()

    wide_specs: list[tuple[int, int, int, int]] = [
        (20261111, 0, 1, 10),
        (20261111, 0, 1 << 32, 11),
        (20261111, 0, (1 << 32) + 1, 12),
        (42, 0, (1 << 31) + 3, 25),
        (42, -(1 << 62), (1 << 62) + 3, 25),
        (20261111, -100, 100, 15),
        (7, -(1 << 63), (1 << 63) - 1, 10),
    ]
    wide = []
    for seed, low, high, count in wide_specs:
        g = np.random.Generator(np.random.PCG64(seed))
        vals = g.integers(low, high, size=count, dtype=np.int64)
        wide.append(
            {
                "seed": str(seed),
                "low": str(low),
                "high": str(high),
                "count": count,
                "values": [str(int(v)) for v in vals],
            }
        )
    return {
        "bootstrap_matrix_sha256": mat_sha256,
        "head3": mat[:3].tolist(),
        "tail3": mat[-3:].tolist(),
        "wide": wide,
    }


def mean_cases() -> dict[str, object]:
    rng = np.random.Generator(np.random.PCG64(999))
    # Generate one 300-element source array, then slice prefixes 1..=300 for compact JSON.
    src_300 = (rng.random(300) - 0.5) * 1e3
    means_1_to_300 = [f64_bits(src_300[:n].mean()) for n in range(1, 301)]

    neg_zero_ns = [1, 7, 8, 9, 16, 129]
    neg_zero_means = [
        f64_bits(np.array([-0.0] * n, dtype=np.float64).mean()) for n in neg_zero_ns
    ]

    mat_100_129 = rng.random((100, 129)) - 0.5
    means_100_129 = [f64_bits(x) for x in mat_100_129.mean(axis=1)]

    # (10000, 12) bootstrap matrix means on synthetic family means.
    fam = (rng.random(12) * 0.9 + 0.05).astype(np.float64)
    idx = np.random.Generator(np.random.PCG64(20261111)).integers(0, 12, size=(10000, 12))
    draws = fam[idx].mean(axis=1)
    draws_sha256 = hashlib.sha256(draws.astype("<f8").tobytes()).hexdigest()

    return {
        "src_300_bits": [f64_bits(x) for x in src_300],
        "means_1_to_300_bits": means_1_to_300,
        "neg_zero_ns": neg_zero_ns,
        "neg_zero_means_bits": neg_zero_means,
        "mat_100_129_bits": [f64_bits(x) for x in mat_100_129.ravel()],
        "means_100_129_bits": means_100_129,
        "fam_12_bits": [f64_bits(x) for x in fam],
        "draws_10000_sha256": draws_sha256,
        "draws_head5_bits": [f64_bits(x) for x in draws[:5]],
        "draws_tail5_bits": [f64_bits(x) for x in draws[-5:]],
    }


def percentile_cases() -> list[dict[str, object]]:
    rng = np.random.Generator(np.random.PCG64(314159))
    arrays: list[tuple[str, list[float], list[float]]] = [
        ("single", [42.5], [0.0, 2.5, 50.0, 95.0, 97.5, 100.0]),
        ("two_neg_zero_a", [-0.0, 0.0], [0.0, 50.0, 100.0]),
        ("two_neg_zero_b", [0.0, -0.0], [0.0, 50.0, 100.0]),
        ("pos_inf", [1.0, float("inf")], [0.0, 50.0, 100.0]),
        ("neg_inf", [float("-inf"), 1.0], [0.0, 50.0, 100.0]),
        ("both_inf", [float("-inf"), float("inf")], [0.0, 50.0, 100.0]),
        ("with_nan", [1.0, float("nan"), 2.0], [0.0, 50.0, 100.0]),
        ("only_nan", [float("nan")], [0.0, 50.0, 100.0]),
        ("len2", [10.0, 20.0], [0.0, 2.5, 25.0, 50.0, 75.0, 95.0, 97.5, 100.0]),
        ("len12", rng.normal(0.0, 10.0, size=12).tolist(), [0.0, 2.5, 50.0, 95.0, 97.5, 100.0]),
        ("len100", rng.random(100).tolist(), [2.5, 95.0, 97.5]),
        ("len10000_ints", rng.integers(0, 5000, size=10000).astype(np.float64).tolist(), [2.5, 95.0, 97.5]),
    ]
    out = []
    for name, arr, qs in arrays:
        np_arr = np.asarray(arr, dtype=np.float64)
        res_multi = np.percentile(np_arr, qs, method="linear")
        res_single = [float(np.percentile(np_arr, q, method="linear")) for q in qs]
        out.append(
            {
                "name": name,
                "values_bits": [f64_bits(x) for x in arr],
                "qs": qs,
                "multi_bits": [f64_bits(x) for x in res_multi],
                "single_bits": [f64_bits(x) for x in res_single],
            }
        )
    return out


def dot_cases() -> list[dict[str, object]]:
    rng = np.random.Generator(np.random.PCG64(271828))
    diffs = rng.normal(0.0, 0.25, size=12)
    out = []
    for mask in [0, 1, 0b101010101010, 0xFFF]:
        signs = np.array([-1.0 if ((mask >> i) & 1) else 1.0 for i in range(12)], dtype=np.float64)
        out.append(
            {
                "a_bits": [f64_bits(x) for x in signs],
                "b_bits": [f64_bits(x) for x in diffs],
                "dot_bits": f64_bits(float(np.dot(signs, diffs))),
            }
        )
    return out


def synthetic_bootstrap_case() -> dict[str, object]:
    rng_data = np.random.Generator(np.random.PCG64(777))
    models = {
        "alpha": rng_data.uniform(0.3, 0.9, size=12).tolist(),
        "beta": rng_data.uniform(0.2, 0.8, size=12).tolist(),
        "gamma": rng_data.uniform(0.4, 0.95, size=12).tolist(),
    }
    model_names = sorted(models)
    sampled = np.random.Generator(np.random.PCG64(20261111)).integers(
        0, 12, size=(10000, 12)
    )
    intervals = {}
    model_draws = {}
    means = {}
    for m in model_names:
        vec = np.asarray(models[m], dtype=np.float64)
        mean = float(vec.mean())
        draws = vec[sampled].mean(axis=1)
        lo, hi = np.percentile(draws, [2.5, 97.5], method="linear")
        means[m] = mean
        model_draws[m] = draws
        intervals[m] = {
            "mean_bits": f64_bits(mean),
            "low_bits": f64_bits(float(lo)),
            "high_bits": f64_bits(float(hi)),
        }
    paired = []
    for i, ma in enumerate(model_names):
        for mb in model_names[i + 1 :]:
            diff = means[ma] - means[mb]
            draws = model_draws[ma] - model_draws[mb]
            lo, hi = np.percentile(draws, [2.5, 97.5], method="linear")
            paired.append(
                {
                    "model_a": ma,
                    "model_b": mb,
                    "diff_bits": f64_bits(diff),
                    "low_bits": f64_bits(float(lo)),
                    "high_bits": f64_bits(float(hi)),
                }
            )
    return {
        "models_bits": {m: [f64_bits(x) for x in v] for m, v in models.items()},
        "intervals": intervals,
        "paired": paired,
    }


payload = {
    "seed_sequence": seed_sequence_cases(),
    "pcg64": pcg64_cases(),
    "integers": integers_cases(),
    "mean": mean_cases(),
    "percentile": percentile_cases(),
    "dot": dot_cases(),
    "bootstrap": synthetic_bootstrap_case(),
}
text = json.dumps(payload, ensure_ascii=True) + "\n"
OUT.write_text(text)
print(f"wrote {OUT} ({len(text)} bytes)")
