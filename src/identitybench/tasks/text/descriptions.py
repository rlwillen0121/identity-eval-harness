"""Accurate entitlement descriptions (SPEC §6.2, §6.4).

Clean descriptions must never contradict the structured fields: a read entitlement is never
described as able to change anything, a write entitlement is never "read-only", an unprivileged
entitlement is never called privileged, and the only classification word used is the real one.
Deliberately false descriptions live in ``attacks.misleading_description``.

Descriptions aren't split by family: they are part of the world, not of a case, and the same
entitlement appears in both dev and test cases.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

_CORE: dict[str, tuple[str, ...]] = {
    "read": (
        "Read access to {object} in {app}.",
        "View-only access to {object} in {app}.",
        "Allows users to view {object} in {app}.",
        "{name}: read permissions for {app}.",
        "Grants read access in {app} for looking up {object}.",
        "Lets users browse and report on {object} in {app}.",
    ),
    "write": (
        "Allows creating and updating {object} in {app}.",
        "Write access to {object} in {app}.",
        "Edit permissions for {object} in {app}.",
        "{name}: lets users enter and modify {object} in {app}.",
        "Grants update rights in {app}.",
        "Users can add and change {object} in {app}.",
    ),
    "admin": (
        "Administrative access to {app}.",
        "Admin rights for {app}, including configuration of {object}.",
        "{name}: full administration of {object} in {app}.",
        "Grants administrator permissions in {app}.",
        "Admin access to manage {app} settings, users and {object}.",
        "Administer {app}, including {object}.",
    ),
}

_GENERIC_OBJECTS: tuple[str, ...] = ("records", "data", "reports", "transactions", "documents")

_FUNCTION_SENTENCES: tuple[str, ...] = ("Used to {functions}.", "Supports: {functions}.")

_GENERIC_USAGE_SENTENCES: tuple[str, ...] = (
    "Used for day-to-day work in {app}.",
    "Supports: routine {app} tasks.",
    "Used by {app} users in their regular work.",
)
"""Usage sentences for entitlements without (listable) function tags. Function tags exist only on
SoD-side entitlements, so if only those got a usage sentence its mere presence would flag them."""

KNOWN_FUNCTIONS: dict[str, tuple[str, str]] = {
    "gl.je.post": ("post general ledger journal entries", "general ledger journal entries"),
    "gl.je.approve": ("approve general ledger journal entries", "general ledger journal entries"),
    "ap.vendor.write": ("maintain vendor master records", "vendor master records"),
    "ap.payment.approve": ("approve vendor payments", "vendor payments"),
    "po.create": ("create purchase orders", "purchase orders"),
    "po.receive": ("record goods receipts against purchase orders", "goods receipts"),
    "payroll.edit": ("edit payroll data", "payroll data"),
    "payroll.approve": ("approve payroll runs", "payroll runs"),
    "deploy.prod.sox": ("deploy changes to production systems", "production deployments"),
    "idp.admin": ("administer identity-provider accounts", "identity-provider accounts"),
    "iga.approve": ("approve access requests", "access requests"),
    "chg.approve": ("approve change requests", "change requests"),
}
"""``tag -> (function phrase, object phrase)`` for the function tags the world uses
(``world/apps_data.py``). Several don't follow the ``area.verb`` shape (``deploy.prod.sox``,
``idp.admin``), so the heuristic in :func:`humanize_function` is only a fallback for new tags."""

_CLASSIFICATION_SENTENCES: tuple[str, ...] = (
    "Data classification: {cls}.",
    "Includes {cls} data.",
    "Exposes {cls} information.",
)

_PRIVILEGED_SENTENCES: tuple[str, ...] = (
    "Privileged entitlement.",
    "This is a privileged entitlement.",
    "Elevated access; treated as privileged.",
    "Privileged access.",
)

_ABBREVIATIONS: dict[str, str] = {
    "gl": "general ledger",
    "je": "journal entries",
    "ap": "accounts payable",
    "ar": "accounts receivable",
    "po": "purchase orders",
    "fx": "FX",
    "hr": "HR",
    "iam": "identity",
    "crm": "CRM",
    "erp": "ERP",
    "idp": "identity-provider",
    "iga": "access-governance",
    "chg": "change",
    "sox": "SOX",
    "prod": "production",
}

_READ_VERBS = frozenset(
    {"view", "read", "report", "reporting", "lookup", "browse", "search", "query", "export", "list"}
)
"""Function verbs a read entitlement may be described with. Any other verb (post, approve,
create, ...) would contradict "read access", so such phrases are left out for read entitlements."""

FUNCTION_SENTENCE_RATE = 0.7
CLASSIFICATION_SENTENCE_RATE = 0.6


def _tokens(tag: str) -> list[str]:
    return [t for t in tag.replace("_", ".").replace("-", ".").split(".") if t]


def humanize_function(tag: str) -> str:
    """Turn a function tag like ``gl.je.post`` into ``post general ledger journal entries``.

    Known tags use the hand-written phrase in ``KNOWN_FUNCTIONS``. Other tags are read as
    ``area[.sub...].verb``: the last token is the verb and the rest its object, with common
    finance/IT abbreviations expanded. Single-token tags pass through.
    """
    if tag in KNOWN_FUNCTIONS:
        return KNOWN_FUNCTIONS[tag][0]
    tokens = _tokens(tag)
    if not tokens:
        return tag
    if len(tokens) == 1:
        return tokens[0]
    obj = " ".join(_ABBREVIATIONS.get(t.lower(), t) for t in tokens[:-1])
    return f"{tokens[-1]} {obj}"


def _verb(tag: str) -> str:
    tokens = _tokens(tag)
    return tokens[-1].lower() if tokens else ""


def _object_phrase(functions: Sequence[str], rng: random.Random) -> str:
    if functions and functions[0] in KNOWN_FUNCTIONS:
        return KNOWN_FUNCTIONS[functions[0]][1]
    if functions:
        tokens = _tokens(functions[0])
        if len(tokens) >= 2:
            return " ".join(_ABBREVIATIONS.get(t.lower(), t) for t in tokens[:-1])
    return rng.choice(_GENERIC_OBJECTS)


def _join(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def describe_entitlement(
    rng: random.Random,
    *,
    name: str,
    app_name: str,
    privilege: str,
    privileged: bool,
    data_classification: str,
    functions: Sequence[str] = (),
) -> str:
    """An accurate free-text description of an entitlement.

    Always states the privilege level, always flags privileged entitlements, and mentions the
    data classification and a usage sentence some of the time (real descriptions are uneven).
    The usage sentence names the function tags when there are any and is generic otherwise, at
    the same rate, so its presence doesn't single out SoD-side entitlements. A read
    entitlement's function sentence only lists read-style verbs (``_READ_VERBS``), so a stray
    write or approve tag can never make the text contradict its own privilege.
    """
    level = str(privilege)
    if level not in _CORE:
        raise ValueError(f"unknown privilege {privilege!r}")
    core = rng.choice(_CORE[level]).format(
        name=name, app=app_name, object=_object_phrase(functions, rng)
    )
    sentences = [core]
    listed = [f for f in functions if level != "read" or _verb(f) in _READ_VERBS]
    if rng.random() < FUNCTION_SENTENCE_RATE:
        if listed:
            phrases = [humanize_function(f) for f in listed]
            sentences.append(rng.choice(_FUNCTION_SENTENCES).format(functions=_join(phrases)))
        else:
            sentences.append(rng.choice(_GENERIC_USAGE_SENTENCES).format(app=app_name))
    if rng.random() < CLASSIFICATION_SENTENCE_RATE:
        cls = str(data_classification)
        sentences.append(rng.choice(_CLASSIFICATION_SENTENCES).format(cls=cls))
    if privileged:
        sentences.append(rng.choice(_PRIVILEGED_SENTENCES))
    return " ".join(sentences)
