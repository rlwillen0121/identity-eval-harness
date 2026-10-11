"""Surface noise: determinism, protected acronyms and the kinds of edits it makes."""

from __future__ import annotations

import random

from identitybench.tasks.text.noise import add_noise, noisify_segments, typo

TEXT = (
    "Please add me because I need this as soon as possible for my manager's account work, "
    "thanks. Information about the VPN and ERP tomorrow."
)


def test_deterministic() -> None:
    a = [add_noise(random.Random(s), TEXT) for s in range(50)]
    b = [add_noise(random.Random(s), TEXT) for s in range(50)]
    assert a == b


def test_typo_changes_word_but_keeps_first_letter() -> None:
    rng = random.Random(0)
    for _ in range(500):
        word = rng.choice(["reconcile", "access", "report", "ledger", "quarter"])
        out = typo(rng, word)
        assert out[0] == word[0]
        assert abs(len(out) - len(word)) <= 1


def test_short_words_are_left_alone() -> None:
    assert typo(random.Random(0), "ok") == "ok"


def test_acronyms_never_mangled() -> None:
    for seed in range(400):
        out = add_noise(random.Random(seed), TEXT)
        assert "VPN" in out or "vpn" in out
        assert "ERP" in out or "erp" in out


def test_shorthand_appears() -> None:
    outs = [add_noise(random.Random(s), TEXT) for s in range(200)]
    for token in ("pls", "b/c", "asap", "mgr", "acct", "thx", "info", "tmrw"):
        assert any(token in o for o in outs), token


def test_casing_and_punctuation_vary() -> None:
    outs = [add_noise(random.Random(s), TEXT) for s in range(300)]
    assert any(o == o.lower() for o in outs)
    assert any(o.endswith("tomorrow") or o.endswith("tmrw") for o in outs)
    assert any(o.endswith("!") for o in outs)


def test_segments_keep_their_count() -> None:
    rng = random.Random(1)
    for _ in range(100):
        assert len(noisify_segments(rng, ["Need ", " in ", "."])) == 3


def test_rates_are_low() -> None:
    """Most words survive: noise should look messy, not unreadable."""
    words = TEXT.split()
    changed = 0
    for seed in range(200):
        out = add_noise(random.Random(seed), TEXT).lower().split()
        changed += sum(1 for w in words if w.lower() not in out)
    assert changed / (200 * len(words)) < 0.2
