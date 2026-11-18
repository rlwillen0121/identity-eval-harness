"""Regenerate CPython golden vectors for ib-core.

Run: uv run python crates/ib-core/tests/fixtures/gen_pycompat_vectors.py
"""

from __future__ import annotations

import json
import random
import struct
from pathlib import Path

OUT = Path(__file__).with_name("pycompat_vectors.json")


def floats() -> list[dict[str, object]]:
    rng = random.Random(20261116)
    vals: list[float] = [
        0.0,
        -0.0,
        1.0,
        0.1,
        1e16,
        1e15,
        9999999999999998.0,
        1e-4,
        1e-5,
        5e-324,
        1.7976931348623157e308,
        0.5,
        2.5,
        1 / 3,
        2 / 3,
        100.0,
        123456.789,
        1e22,
        1e23,
        0.30000000000000004,
    ]
    for _ in range(4000):
        bits = rng.getrandbits(64)
        f = struct.unpack("<d", struct.pack("<Q", bits))[0]
        if f == f and f not in (float("inf"), float("-inf")):
            vals.append(f)
    for _ in range(2000):
        vals.append(round(rng.uniform(-1000, 1000), rng.randint(0, 8)))
        vals.append(rng.random())
    out = []
    for f in vals:
        out.append(
            {
                "bits": struct.unpack("<Q", struct.pack("<d", f))[0],
                "repr": repr(f),
                "fixed2": f"{f:.2f}" if abs(f) < 1e30 else None,
                "fixed4": f"{f:.4f}" if abs(f) < 1e30 else None,
                "round3": repr(round(f, 3)) if abs(f) < 1e300 else None,
            }
        )
    return out


def rng_cases() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    seeds: list[object] = [
        0,
        1,
        42,
        2**32,
        2**64 - 1,
        17565442252895372630,
        -7,
        "abc",
        "",
        "records:123",
        "v5-slice-map\0v5_dev_ep_03142c89c305fc75cbb0",
        "é✓",
    ]
    for seed in seeds:
        r = random.Random(seed)
        randoms = [r.random() for _ in range(5)]
        bits = [r.getrandbits(k) for k in (1, 7, 32, 33, 64, 100)]
        below = [r.randrange(n) for n in (1, 2, 3, 10, 1000, 2**40 + 3)]
        x = list(range(37))
        r.shuffle(x)
        s_small = r.sample(range(30), 7)
        s_big = r.sample(range(5000), 9)
        choice = r.choice(list(range(11)))
        uni = r.uniform(-3.0, 9.0)
        ch = r.choices(list(range(13)), k=6)
        cases.append(
            {
                "seed_kind": "str" if isinstance(seed, str) else "int",
                "seed": seed if isinstance(seed, str) else str(seed),
                "random": [repr(v) for v in randoms],
                "getrandbits": [str(b) for b in bits],
                "randrange": [str(b) for b in below],
                "shuffle37": x,
                "sample7of30": s_small,
                "sample9of5000": s_big,
                "choice11": choice,
                "uniform": repr(uni),
                "choices6of13": ch,
            }
        )
    return cases


def strings() -> list[dict[str, str]]:
    samples = [
        "plain",
        'quote"back\\slash',
        "ctl\x00\x01\x1f\x7f",
        "tab\tnl\nret\r\b\f",
        "é✓😀",
        "  ",
        "/slash",
    ]
    return [
        {"s": s, "ascii": json.dumps(s), "noascii": json.dumps(s, ensure_ascii=False)}
        for s in samples
    ]


OUT.write_text(
    json.dumps({"floats": floats(), "rng": rng_cases(), "strings": strings()}, ensure_ascii=True)
    + "\n"
)
print(f"wrote {OUT}")
