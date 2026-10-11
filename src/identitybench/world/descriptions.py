"""Per-world entitlement descriptions (SPEC §6.2, §6.4, §9.4).

An entitlement's description is free text a model reads next to the structured fields. Written
once per catalog entry, it would be the same in every world of a profile, and two things would
follow. Its length would track policy: administrator seats would carry long hand-written
sentences and generated module entitlements short template ones, so length would follow
privilege, risk tier, requestability and SoD side. And its length would name the entitlement in
every world, so a classifier trained on dev worlds could learn each entitlement's label rates
and read them off in test worlds by length alone (with fixed catalog text, a probe given only
the requested entitlement's description length beats the majority class by about 13 points on
fresh request cases). So each world words its catalog afresh:

- **The length comes first.** Every entitlement draws a target length from one distribution
  (``target_length``), the same for every kind of entitlement, so within a world the length
  says nothing about risk, privilege, requestability or SoD side, and across worlds the same
  entitlement lands on a different length.
- **The wording is assembled to fit it.** A core sentence says what the access does; zero to
  three context sentences (how the access is assigned, where it shows up, who to ask) fill
  out the rest, and a core may be led by the entitlement's name. The candidate closest to the
  target wins, ties drawn at random. Every entitlement but an SoD side's has short generic
  cores ("Read-only access."), and context sentences reach past the longest targets, so every
  kind of entitlement lands within a few characters of every target;
  ``tests/world/test_descriptions.py`` checks that over every catalog entry and a grid of
  targets.
- **Specific beats generic.** A candidate whose core says specifically what the access does
  (the hand-written sentence from ``apps_data`` or a shortened form of it, a module's verb
  phrase) is preferred whenever one lands within ``SPECIFIC_SLACK`` characters of the target;
  only then does a generic core stand in. An entitlement on an SoD side has no generic core:
  its description always leads with its function's phrase (``SOD_PHRASES``, short enough for
  the shortest targets), so the rule's sides read as what they are.

Descriptions never state a policy fact a structured field carries: no risk tier, data
classification, privileged flag, requestability, eligible employment types, maximum duration,
SOX scope or SoD membership. Core phrases match the privilege (a read entitlement is never
described as changing anything, an approver never as creating what it approves); generic cores
state only the privilege level, and context sentences fit any entitlement. No frame contains a
phrase ``tests/world/test_catalog`` reads as an SoD capability, so a description can't make a
non-side entitlement look like a rule's side.

Wording draws from its own ``random.Random``, seeded once from the world's stream
(``catalog.build_catalog``), so changing a frame changes descriptions and nothing else.
"""

from __future__ import annotations

import random
import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from identitybench.world.entities import Entitlement, Privilege

TARGET_MIN = 30
TARGET_MAX = 170
TARGET_SHAPE = 2.0
TARGET_SCALE = 18.0
"""Target lengths are ``TARGET_MIN`` plus a gamma-distributed excess (shape 2, scale 18),
capped at ``TARGET_MAX``: mostly one or two short sentences (mode about 48 characters, mean
about 66, close to the hand-written sentences' own), sometimes a short paragraph, as real
catalogs are uneven."""

SPECIFIC_SLACK = 4
"""A specific core is used whenever one of its candidates lands this close to the target."""
TIE_SLACK = 2
"""Candidates this much further from the target than the best are drawn among at random, so
the same entitlement and target don't always get the same words."""
CONTEXTS_PER_POOL = 2
"""Context sentences sampled from each of the short, medium and long pools per entitlement."""
MAX_CONTEXTS = 3
"""Most context sentences a description carries."""


@dataclass(frozen=True, slots=True)
class Wording:
    """What the catalog knows an entitlement does: the material its description is made of.

    Hand-written entitlements carry their ``apps_data`` sentence (``detail``); generated module
    entitlements carry the module's action and plural noun phrase. Regional and UAT copies
    share their base entitlement's wording and are worded with their own application's name.
    """

    detail: str | None = None
    action: str | None = None
    object: str | None = None


SOD_PHRASES: Mapping[str, tuple[str, ...]] = {
    "ap.vendor.write": ("maintain vendor records", "create and edit vendor master records"),
    "ap.payment.approve": ("approve vendor payments", "approve vendor payment batches"),
    "gl.je.post": ("post journal entries", "create and post general ledger journal entries"),
    "gl.je.approve": (
        "approve journal entries",
        "review and approve general ledger journal entries",
    ),
    "chg.approve": ("approve change requests", "approve normal and emergency change requests"),
    "deploy.prod.sox": ("deploy to production", "promote releases to production"),
    "idp.admin": ("manage user accounts", "create, modify and deactivate user accounts"),
    "iga.approve": ("approve access requests", "approve or reject access requests"),
    "payroll.edit": ("edit pay elements", "edit pay rates and other payroll data"),
    "payroll.approve": ("approve payroll runs", "approve and finalize payroll runs"),
    "po.create": ("issue purchase orders", "raise and issue purchase orders"),
    "po.receive": ("record receipt of goods", "record receipt of goods against purchase orders"),
}
"""Bare-infinitive phrases for each SoD function tag (``specs.SOD_DEFINITIONS``), short and
long. Each reads as the side it is, so an SoD-side description always shows its function."""

_MODULE_PHRASES: Mapping[str, tuple[str, ...]] = {
    "view": (
        "view {object}",
        "look up {object}",
        "browse and search {object}",
        "open and read {object}",
    ),
    "edit": (
        "create and update {object}",
        "create, update and delete {object}",
        "edit {object}",
        "maintain {object}",
        "add, change and remove {object}",
    ),
    "approve": (
        "approve pending changes to {object}",
        "review and approve changes to {object}",
        "sign off on proposed changes to {object}",
    ),
    "export": (
        "export {object}",
        "export {object} to CSV files",
        "download {object} as spreadsheets",
        "run bulk exports of {object}",
    ),
}
"""Verb phrases per module action (``catalog`` generates these four). Approvers are never said
to create or edit (that would read like the other side of an approval), and read actions
never change anything."""

_MODULE_SENTENCES: Mapping[str, tuple[str, ...]] = {
    "view": ("Read-only access to {object}.", "Read-only access to {object} in {app}."),
    "edit": ("Edit rights on {object}.", "Editor access for {object} in {app}."),
    "approve": ("Approver for changes to {object}.", "Approver for changes to {object} in {app}."),
    "export": ("Export access for {object}.", "Export access for {object} in {app}."),
}
"""Noun-style core sentences per module action, for variety beside the verb frames."""

_VERB_FRAMES: tuple[str, ...] = (
    "{Vp}.",
    "{Vp} in {app}.",
    "Lets the holder {vp}.",
    "Lets the holder {vp} in {app}.",
    "Allows users to {vp}.",
    "Allows users to {vp} in {app}.",
    "Holders can {vp} in {app}.",
    "Used to {vp}.",
    "Grants the ability to {vp} in {app}.",
)
"""Sentences around a bare-infinitive phrase. Frames naming the app are skipped when the
phrase already says where (" in ") or shares a word with the app name ("view CRM reports in
CRM")."""

_GENERIC_CORES: Mapping[Privilege, tuple[str, ...]] = {
    Privilege.read: (
        "Read-only access.",
        "View-only access.",
        "Read access in {app}.",
        "Read-only access to {app}.",
        "View access for {app}.",
    ),
    Privilege.write: (
        "Write access.",
        "Edit access.",
        "Write access in {app}.",
        "Can make changes in {app}.",
        "Standard write access to {app}.",
    ),
    Privilege.admin: (
        "Admin access.",
        "Administrator access.",
        "Admin access to {app}.",
        "Administrative rights in {app}.",
        "Administers {app}.",
    ),
}
"""Core sentences that state only the privilege level, for targets too short for a specific
core. They cover every target from about 12 characters up."""

_CONTEXTS: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] = (
    (
        "Assigned per user.",
        "Per-user assignment.",
        "Web and API access.",
        "Effective at next sign-in.",
        "Activity is logged.",
        "Synced hourly.",
        "Managed by the app owner.",
    ),
    (
        "Takes effect the next time the holder signs in.",
        "Covers both the web interface and the API.",
        "Maps to a single group in {app}.",
        "Granted on the holder's own {app} account.",
        "Part of the standard {app} permission set.",
        "Used in day-to-day {app} work.",
        'Shown as "{name}" in {app}.',
        "Synced to {app} by the provisioning connector.",
        "Questions go to the {app} application owner.",
        "See the {app} wiki page for details.",
    ),
    (
        "Activity under this entitlement appears in the {app} audit log.",
        "The permission name in {app} may differ slightly; the connector maps it.",
        "Holders see the related menus and pages in {app} after their session refreshes.",
        "Replaces an older entitlement retired during the last {app} migration.",
        "Mirrors the permission of the same name in {app} and is kept in sync by the connector.",
        "The {app} documentation lists exactly which screens this permission unlocks.",
        "Removal takes effect at the next connector sync, usually within the hour.",
    ),
)
"""Short, medium and long context sentences: how the access is assigned and kept in sync,
where it shows up, who to ask. None is privilege-specific or states a policy fact, so any of
them fits any entitlement."""

_CLAUSE_BREAKS: tuple[str, ...] = (": ", ", including ", "; ", ", such as ")
"""Where a hand-written sentence can be cut and still say what the access does ("View finance
dashboards: P&L, cash and spend." -> "View finance dashboards.")."""

_WORD = re.compile(r"[a-z0-9]+")
_FILLER = frozenset({"a", "an", "and", "for", "in", "of", "on", "the", "to"})


@dataclass(frozen=True, slots=True)
class _Core:
    text: str
    specific: bool


def target_length(rng: random.Random) -> int:
    """A description's target length in characters (see ``TARGET_SHAPE``)."""
    excess = round(rng.gammavariate(TARGET_SHAPE, TARGET_SCALE))
    return min(TARGET_MAX, TARGET_MIN + excess)


def describe_catalog(
    entitlements: Mapping[str, Entitlement],
    wordings: Mapping[str, Wording],
    app_names: Mapping[str, str],
    rng: random.Random,
) -> dict[str, Entitlement]:
    """``entitlements`` with every description worded for this world, in ID order (so the
    draws don't depend on the order the catalog was built in)."""
    worded: dict[str, Entitlement] = {}
    for ent_id in sorted(entitlements):
        ent = entitlements[ent_id]
        text = describe(ent, wordings[ent_id], app_names[ent.app_id], target_length(rng), rng)
        worded[ent_id] = ent.model_copy(update={"description": text})
    return {ent_id: worded[ent_id] for ent_id in entitlements}


def describe(
    ent: Entitlement, wording: Wording, app_name: str, target: int, rng: random.Random
) -> str:
    """The candidate description of ``ent`` closest to ``target`` characters.

    Candidates are a core sentence followed by up to ``MAX_CONTEXTS`` context sentences, drawn
    two from each context pool in a random order and kept in that order. Specific cores win
    when one lands within ``SPECIFIC_SLACK`` of the target; an SoD-side entitlement has only
    specific cores. Among the candidates within ``TIE_SLACK`` of the best, one is drawn.
    """
    cores = _cores(ent, wording, app_name)
    contexts = [
        frame.format(app=app_name, name=ent.name)
        for pool in _CONTEXTS
        for frame in rng.sample(pool, CONTEXTS_PER_POOL)
    ]
    rng.shuffle(contexts)
    tails = _tails(contexts)
    extras = [extra for _, extra in tails]

    def miss(core: _Core) -> int:
        """How far the best tail leaves ``core`` from the target."""
        need = target - len(core.text)
        at = bisect_left(extras, need)
        return min(abs(extras[j] - need) for j in (at - 1, at) if 0 <= j < len(extras))

    specific = [core for core in cores if core.specific]
    pool = cores
    if specific and min(miss(core) for core in specific) <= SPECIFIC_SLACK:
        pool = specific
    limit = min(miss(core) for core in pool) + TIE_SLACK
    candidates: list[tuple[_Core, tuple[str, ...]]] = []
    for core in pool:
        need = target - len(core.text)
        low, high = bisect_left(extras, need - limit), bisect_right(extras, need + limit)
        candidates += [(core, tails[j][0]) for j in range(low, high)]
    core, tail = rng.choice(candidates)
    return " ".join((core.text, *tail))


def _tails(contexts: list[str]) -> list[tuple[tuple[str, ...], int]]:
    """Every in-order selection of up to ``MAX_CONTEXTS`` sentences with the characters it adds
    (each sentence plus its separating space), shortest first."""
    tails: list[tuple[tuple[str, ...], int]] = [((), 0)]
    for sentence in contexts:
        tails += [
            ((*tail, sentence), extra + 1 + len(sentence))
            for tail, extra in tails
            if len(tail) < MAX_CONTEXTS
        ]
    return sorted(tails, key=lambda pair: pair[1])


def _cores(ent: Entitlement, wording: Wording, app_name: str) -> list[_Core]:
    """Every core sentence for ``ent``, specific ones first.

    An SoD-side entitlement's cores all lead with its function's phrase (``SOD_PHRASES``),
    alone or followed by the hand-written sentence, and it has no generic core, so its side
    always shows. Other entitlements get their module phrases or hand-written sentence (and
    its shortened forms), each also led by the entitlement's name, then the generic cores for
    their privilege level.
    """
    sod_phrases = [p for f in ent.functions for p in SOD_PHRASES.get(f, ())]
    if sod_phrases:
        leads = _framed(sod_phrases, app_name)
        leads += [f"{ent.name}: {_lower_first(lead)}" for lead in leads]
        details = [wording.detail] if wording.detail is not None else []
        texts = [*leads, *(f"{lead} {detail}" for lead in leads for detail in details)]
        return [_Core(text, True) for text in _unique(texts)]
    phrases: list[str] = []
    sentences: list[str] = []
    if wording.action is not None and wording.object is not None:
        phrases += [p.format(object=wording.object) for p in _MODULE_PHRASES[wording.action]]
        sentences += [
            s.format(object=wording.object, app=app_name)
            for s in _MODULE_SENTENCES[wording.action]
            if "{app}" not in s or not _shares_word(wording.object, app_name)
        ]
    if wording.detail is not None:
        sentences += [wording.detail, *_shortened(wording.detail)]
    specific = [
        *_framed(phrases, app_name),
        *sentences,
        *(f"{ent.name}: {_lower_first(s)}" for s in sentences),
    ]
    generic = [s.format(app=app_name) for s in _GENERIC_CORES[ent.privilege]]
    return [_Core(s, True) for s in _unique(specific)] + [_Core(s, False) for s in generic]


def _framed(phrases: list[str], app_name: str) -> list[str]:
    """Each bare-infinitive phrase in every verb frame that reads well with it."""
    framed = [_verb_sentence(frame, p, app_name) for p in phrases for frame in _VERB_FRAMES]
    return [s for s in framed if s is not None]


def _verb_sentence(frame: str, phrase: str, app_name: str) -> str | None:
    """``frame`` filled with ``phrase``, or ``None`` when naming the app would read badly."""
    if "{app}" in frame and (" in " in phrase or _shares_word(phrase, app_name)):
        return None
    return frame.format(Vp=phrase[:1].upper() + phrase[1:], vp=phrase, app=app_name)


def _shortened(detail: str) -> list[str]:
    """Shorter forms of a hand-written sentence that still say what the access does: its first
    sentence, and the part before a clause break (``_CLAUSE_BREAKS``)."""
    cuts: list[str] = []
    first, _, rest = detail.partition(". ")
    if rest:
        cuts.append(f"{first}.")
    for marker in _CLAUSE_BREAKS:
        head = first.split(marker, 1)[0]
        if head != first and len(head.split()) >= 2:
            cuts.append(f"{head.rstrip('.')}.")
    return cuts


def _lower_first(sentence: str) -> str:
    """``sentence`` with its first letter lowered, unless the first word is an acronym or a
    name ("SSO", "IdP", "EU")."""
    word = sentence.split(" ", 1)[0]
    if word[:1].isupper() and word[1:].islower():
        return sentence[:1].lower() + sentence[1:]
    return sentence


def _shares_word(phrase: str, app_name: str) -> bool:
    """True when a content word of ``phrase`` is also in the app name (crude singular)."""

    def words(text: str) -> set[str]:
        return {
            w[:-1] if len(w) > 3 and w.endswith("s") else w
            for w in _WORD.findall(text.lower())
            if w not in _FILLER
        }

    return not words(phrase).isdisjoint(words(app_name))


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))
