"""Surface noise for free text: typos, shorthand, casing and punctuation (SPEC §6.4).

Noise is applied per template to the literal segments only, so slot values (app and entitlement
names, ticket IDs) are never corrupted. Rates are low on purpose: the goal is a messy directory,
not unreadable text. Only lowercase words get typos, which keeps acronyms such as "VPN" from
being mangled into something else.
"""

from __future__ import annotations

import random
import re

SHORTHAND: tuple[tuple[str, str], ...] = (
    ("as soon as possible", "asap"),
    ("please", "pls"),
    ("with", "w/"),
    ("because", "b/c"),
    ("account", "acct"),
    ("manager", "mgr"),
    ("thanks", "thx"),
    ("information", "info"),
    ("tomorrow", "tmrw"),
    ("you", "u"),
)
"""(phrase, shorthand) pairs, each replaced independently with probability ``SHORTHAND_RATE``."""

TYPO_RATE = 0.025
SHORTHAND_RATE = 0.35
LOWERCASE_ALL_RATE = 0.15
LOWERCASE_FIRST_RATE = 0.07
DROP_FINAL_PERIOD_RATE = 0.25
EXCLAIM_RATE = 0.05

_SHORTHAND_PATTERNS = tuple(
    (re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE), repl) for phrase, repl in SHORTHAND
)
_TYPO_WORD = re.compile(r"\b[a-z]{4,}\b")
_NEIGHBORS: dict[str, str] = {
    "a": "sqw",
    "b": "vng",
    "c": "xvd",
    "d": "sfe",
    "e": "wrd",
    "f": "dgr",
    "g": "fht",
    "h": "gjy",
    "i": "uok",
    "j": "hku",
    "k": "jli",
    "l": "kop",
    "m": "nj",
    "n": "bmh",
    "o": "ipl",
    "p": "ol",
    "q": "wa",
    "r": "etf",
    "s": "adw",
    "t": "ryg",
    "u": "yij",
    "v": "cbf",
    "w": "qes",
    "x": "zcs",
    "y": "tuh",
    "z": "xa",
}


def typo(rng: random.Random, word: str) -> str:
    """One keyboard-style slip: swap, drop, double or neighbor-key. Keeps the first letter."""
    if len(word) < 3:
        return word
    i = rng.randrange(1, len(word) - 1)
    op = rng.randrange(4)
    if op == 0:
        return word[:i] + word[i + 1] + word[i] + word[i + 2 :]
    if op == 1:
        return word[:i] + word[i + 1 :]
    if op == 2:
        return word[:i] + word[i] + word[i:]
    return word[:i] + rng.choice(_NEIGHBORS.get(word[i], word[i])) + word[i + 1 :]


def _shorthand(rng: random.Random, text: str) -> str:
    for pattern, repl in _SHORTHAND_PATTERNS:
        text = pattern.sub(lambda m, r=repl: r if rng.random() < SHORTHAND_RATE else m[0], text)
    return text


def _typos(rng: random.Random, text: str) -> str:
    return _TYPO_WORD.sub(lambda m: typo(rng, m[0]) if rng.random() < TYPO_RATE else m[0], text)


def noisify_segments(rng: random.Random, segments: list[str]) -> list[str]:
    """Add noise to a template's literal segments; casing decisions apply to the whole text."""
    casing = rng.random()
    out = [_typos(rng, _shorthand(rng, seg)) for seg in segments]
    if casing < LOWERCASE_ALL_RATE:
        out = [seg.lower() for seg in out]
    elif casing < LOWERCASE_ALL_RATE + LOWERCASE_FIRST_RATE and out and out[0]:
        out[0] = out[0][0].lower() + out[0][1:]
    if out and out[-1].endswith("."):
        r = rng.random()
        if r < DROP_FINAL_PERIOD_RATE:
            out[-1] = out[-1][:-1]
        elif r < DROP_FINAL_PERIOD_RATE + EXCLAIM_RATE:
            out[-1] = out[-1][:-1] + "!"
    return out


def add_noise(rng: random.Random, text: str) -> str:
    """Noise a whole string (for text that has no protected slot values)."""
    return noisify_segments(rng, [text])[0]
