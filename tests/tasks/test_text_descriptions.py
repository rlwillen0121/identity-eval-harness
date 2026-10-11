"""Accurate entitlement descriptions never contradict the structured fields and read as English."""

from __future__ import annotations

import itertools
import random
import re
import string
from collections.abc import Callable, Iterator

import pytest

from identitybench.tasks.text import descriptions
from identitybench.tasks.text.descriptions import (
    APPROVING_FUNCTIONS,
    KNOWN_FUNCTIONS,
    FunctionPhrase,
    describe_entitlement,
    function_phrase,
    humanize_function,
)
from identitybench.world.apps_data import APP_SPECS
from identitybench.world.entities import Application, Entitlement
from identitybench.world.generator import build_world

CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")
CHANGE_WORDS = re.compile(
    r"admin|write|edit|modif|creat|updat|manage|change|add |enter", re.IGNORECASE
)
READ_ONLY_WORDS = re.compile(r"read[- ]only|view[- ]only", re.IGNORECASE)
PRIVILEGED_WORDS = re.compile(r"privileged|elevated", re.IGNORECASE)

READ_TAG = "crm.reports.view"  # a catalog module tag: "view CRM reports"
WRITE_TAG = "gl.je.post"


@pytest.mark.parametrize(
    ("privilege", "privileged", "classification"),
    list(itertools.product(("read", "write", "admin"), (True, False), CLASSIFICATIONS)),
)
def test_descriptions_are_accurate(privilege: str, privileged: bool, classification: str) -> None:
    rng = random.Random(f"{privilege}-{privileged}-{classification}")
    functions = (READ_TAG,) if privilege == "read" else (WRITE_TAG,)
    for _ in range(60):
        text = describe_entitlement(
            rng,
            name="Ledger Access",
            app_name="Northwind Ledger",
            privilege=privilege,
            privileged=privileged,
            data_classification=classification,
            functions=functions,
        )
        assert "{" not in text and "}" not in text
        assert "Northwind Ledger" in text
        if privilege == "read":
            assert not CHANGE_WORDS.search(text), text
        else:
            assert not READ_ONLY_WORDS.search(text), text
        if privilege == "admin":
            assert re.search(r"admin", text, re.IGNORECASE), text
        if privileged:
            assert PRIVILEGED_WORDS.search(text), text
        else:
            assert not PRIVILEGED_WORDS.search(text), text
        for other in CLASSIFICATIONS:
            if other != classification:
                assert other not in text.lower(), text


def test_classification_and_functions_sometimes_mentioned() -> None:
    rng = random.Random(1)
    texts = [
        describe_entitlement(
            rng,
            name="N",
            app_name="A",
            privilege="write",
            privileged=False,
            data_classification="confidential",
            functions=("ap.credit_memos.edit",),
        )
        for _ in range(200)
    ]
    assert any("confidential" in t for t in texts)
    assert any("confidential" not in t for t in texts)
    assert any("create and update credit memos" in t for t in texts)
    assert any("credit memos in A" in t for t in texts)  # the core sentence names the object


def test_function_phrases() -> None:
    assert humanize_function("gl.je.post") == "post general ledger journal entries"
    assert humanize_function("deploy.prod.sox") == "deploy changes to production systems"
    assert humanize_function("idp.admin") == "administer identity-provider accounts"
    assert humanize_function("crm.forecasts.view") == "view sales forecasts"
    assert humanize_function("ap.credit_memos.edit") == "create and update credit memos"
    assert humanize_function("gl.reconciliations.approve") == (
        "approve pending changes to account reconciliations"
    )
    assert humanize_function("chat.private_channels.export") == "export private channels"
    view = function_phrase("crm.forecasts.view")
    assert view is not None and view.reads_only and view.object == "sales forecasts"
    edit = function_phrase("crm.forecasts.edit")
    assert edit is not None and not edit.reads_only
    # Tags derived from entitlement IDs have no reading and come back unchanged.
    for tag in ("ap.invoice_entry", "chat.guest", "ap.admin", "crm.bogus_module.view", "payroll"):
        assert function_phrase(tag) is None
        assert humanize_function(tag) == tag


def test_determinism() -> None:
    def run(seed: int) -> list[str]:
        rng = random.Random(seed)
        return [
            describe_entitlement(
                rng,
                name="N",
                app_name="A",
                privilege="admin",
                privileged=True,
                data_classification="restricted",
            )
            for _ in range(50)
        ]

    assert run(3) == run(3)
    assert run(3) != run(4)


def test_unknown_privilege_rejected() -> None:
    with pytest.raises(ValueError):
        describe_entitlement(
            random.Random(0),
            name="N",
            app_name="A",
            privilege="owner",
            privileged=False,
            data_classification="public",
        )


def test_read_entitlement_never_lists_change_functions() -> None:
    """A stray write/approve tag on a read entitlement must not contradict "read access"."""
    rng = random.Random(2)
    listed_read_verb = False
    for _ in range(300):
        text = describe_entitlement(
            rng,
            name="Payments Viewer",
            app_name="Northwind Ledger",
            privilege="read",
            privileged=False,
            data_classification="internal",
            functions=("ap.payment.approve", "gl.je.post", READ_TAG),
        )
        assert not re.search(r"approve|post\b", text, re.IGNORECASE), text
        listed_read_verb |= "view CRM reports" in text
    assert listed_read_verb


def test_write_entitlement_keeps_change_functions() -> None:
    rng = random.Random(3)
    texts = [
        describe_entitlement(
            rng,
            name="N",
            app_name="A",
            privilege="write",
            privileged=False,
            data_classification="internal",
            functions=("ap.payment.approve",),
        )
        for _ in range(100)
    ]
    assert any("approve vendor payments" in t for t in texts)


def test_object_that_repeats_the_app_name_is_dropped() -> None:
    """ "Administer Accounts Payable, including accounts payable." must not happen."""
    rng = random.Random(4)
    for _ in range(200):
        text = describe_entitlement(
            rng,
            name="Dashboards",
            app_name="BI Dashboards",
            privilege="read",
            privileged=False,
            data_classification="internal",
            functions=("obs.dashboards.view",),  # object "dashboards"
        )
        assert "dashboards in BI" not in text and "on dashboards" not in text, text


SOD_TAGS = sorted({f for app in APP_SPECS for e in app.entitlements for f in e.functions})
BARE_ABBREVIATION = re.compile(r"\b(?:gl|je|ap|ar|po|idp|iga|chg|sox|prod)\b")


def test_every_hand_written_function_tag_has_a_phrase() -> None:
    assert SOD_TAGS, "apps_data defines no explicit function tags"
    assert set(SOD_TAGS) <= set(KNOWN_FUNCTIONS)


@pytest.mark.parametrize("tag", SOD_TAGS)
def test_hand_written_tags_render_as_plain_english(tag: str) -> None:
    phrase, obj = KNOWN_FUNCTIONS[tag]
    assert humanize_function(tag) == phrase
    rng = random.Random(tag)
    texts = [
        describe_entitlement(
            rng,
            name="N",
            app_name="App",
            privilege="write",
            privileged=False,
            data_classification="internal",
            functions=(tag,),
        )
        for _ in range(80)
    ]
    for text in texts:
        assert not BARE_ABBREVIATION.search(text), text
    assert any(phrase in t for t in texts)
    assert any(obj in t for t in texts)


def test_usage_sentence_does_not_single_out_any_kind_of_tag() -> None:
    """Every entitlement gets a usage sentence at the same rate, whatever its tags look like."""

    def usage_rate(functions: tuple[str, ...]) -> float:
        rng = random.Random(5)
        texts = [
            describe_entitlement(
                rng,
                name="N",
                app_name="App",
                privilege="write",
                privileged=False,
                data_classification="internal",
                functions=functions,
            )
            for _ in range(2000)
        ]
        return sum(bool(re.search(r"Used |Holders can|Supports ", t)) for t in texts) / len(texts)

    rates = [usage_rate(f) for f in (("gl.je.post",), ("crm.forecasts.edit",), ("chat.guest",), ())]
    assert max(rates) - min(rates) < 0.05
    assert all(0.6 < r < 0.8 for r in rates)


# ---------------------------------------------------------------- the real catalog

WORLD_PROFILES = ("startup", "midmarket", "enterprise")
"""Enterprise is the only profile whose catalog adds the module ``approve`` and ``export``
actions."""
BROKEN = re.compile(
    r"Used to (?:entry|admin|guest|member|user|agent|viewer|reader|read|write|view_|helpdesk)\b|"
    r"Supports: |admin chat workspace|guest chat|\b(\w+(?: \w+)?) \1\b",
    re.IGNORECASE,
)
"""Outputs the old tag heuristics produced ("Used to entry accounts payable invoice.",
"Supports: admin accounts payable.") and doubled words."""


def _world_entitlements() -> Iterator[tuple[Entitlement, Application]]:
    for profile in WORLD_PROFILES:
        world = build_world(profile, 1)
        for ent in world.entitlements.values():
            yield ent, world.applications[ent.app_id]


@pytest.fixture(scope="module")
def world_entitlements() -> list[tuple[Entitlement, Application]]:
    return list(_world_entitlements())


def _frame(template: str) -> re.Pattern[str]:
    """A template as a regex whose named groups capture the slot values."""
    pattern = ""
    for literal, field, _, _ in string.Formatter().parse(template):
        pattern += re.escape(literal)
        if field is not None:
            pattern += f"(?P<{field}>.+)"
    return re.compile(pattern)


ObjectRule = Callable[[FunctionPhrase], bool]
"""Which functions may supply a frame's {object}."""


def _any_function(phrase: FunctionPhrase) -> bool:
    return True


def _edits(phrase: FunctionPhrase) -> bool:
    return not phrase.approves


def _approves(phrase: FunctionPhrase) -> bool:
    return phrase.approves


def _frames(
    templates: tuple[str, ...], rule: ObjectRule
) -> list[tuple[re.Pattern[str], ObjectRule]]:
    return [(_frame(t), rule) for t in templates]


_CORE_FRAMES: dict[str, list[tuple[re.Pattern[str], ObjectRule]]] = {
    level: _frames(descriptions._CORE[level], _edits if level == "write" else _any_function)
    + _frames(descriptions._CORE_APPROVE.get(level, ()), _approves)
    + _frames(descriptions._CORE_NO_OBJECT[level], _any_function)
    for level in descriptions._CORE
}
"""A write frame that says the holder creates or edits {object} must not name an approver's
object: on gl.je.approve that would describe the gl.je.post side of SOD-002."""
_TAIL_FRAMES = _frames(
    (
        *descriptions._FUNCTION_SENTENCES,
        *descriptions._GENERIC_USAGE_SENTENCES,
        *descriptions._CLASSIFICATION_SENTENCES,
        *descriptions._PRIVILEGED_SENTENCES,
    ),
    _any_function,
)


def _slot_ok(
    slot: str, value: str, ent: Entitlement, app: Application, object_rule: ObjectRule
) -> bool:
    """Each slot may only carry a hand-written value: the names, the real classification, or a
    phrase from KNOWN_FUNCTIONS / the catalog's module table (an object only from a function
    the frame fits)."""
    phrases = [p for p in (function_phrase(f) for f in ent.functions) if p is not None]
    if slot == "app":
        return value == app.name
    if slot == "name":
        return value == ent.name
    if slot == "cls":
        return value == ent.data_classification.value
    if slot == "object":
        app_words = set(re.findall(r"[a-z0-9]+", app.name.lower()))
        return any(
            value == p.object
            and object_rule(p)
            and not set(re.findall(r"[a-z0-9]+", value.lower())) <= app_words
            for p in phrases
        )
    if slot == "functions":
        allowed = [p.verb_phrase for p in phrases if ent.privilege.value != "read" or p.reads_only]
        return value in allowed
    return False


def _sentence_ok(
    sentence: str,
    frames: list[tuple[re.Pattern[str], ObjectRule]],
    ent: Entitlement,
    app: Application,
) -> bool:
    for frame, rule in frames:
        match = frame.fullmatch(sentence)
        if match and all(_slot_ok(k, v, ent, app, rule) for k, v in match.groupdict().items()):
            return True
    return False


def test_catalog_names_split_cleanly_into_sentences(
    world_entitlements: list[tuple[Entitlement, Application]],
) -> None:
    for ent, app in world_entitlements:
        assert ". " not in ent.name and ". " not in app.name, (ent.name, app.name)


def test_every_catalog_tag_is_described_in_correct_english(
    world_entitlements: list[tuple[Entitlement, Application]],
) -> None:
    """Every sentence of every description of every catalog entitlement (all three profiles) is
    a hand-written frame filled with a hand-written value (round-3 fix: real worlds tag every
    entitlement, mostly with module and ID-derived tags, not just the 12 SoD ones)."""
    assert len(world_entitlements) > 5000
    assert all(ent.functions for ent, _ in world_entitlements)
    for index, (ent, app) in enumerate(world_entitlements):
        rng = random.Random(index)
        for _ in range(4):
            text = describe_entitlement(
                rng,
                name=ent.name,
                app_name=app.name,
                privilege=ent.privilege.value,
                privileged=ent.privileged,
                data_classification=ent.data_classification.value,
                functions=ent.functions,
            )
            assert not BROKEN.search(text), text
            core, *tail = text.split(". ")
            sentences = [core + ("." if tail else "")] + [
                s + ("." if i < len(tail) - 1 else "") for i, s in enumerate(tail)
            ]
            assert _sentence_ok(sentences[0], _CORE_FRAMES[ent.privilege.value], ent, app), (
                ent.id,
                ent.functions,
                text,
            )
            for sentence in sentences[1:]:
                assert _sentence_ok(sentence, _TAIL_FRAMES, ent, app), (ent.id, sentence, text)


def test_catalog_module_tags_all_get_a_specific_phrase(
    world_entitlements: list[tuple[Entitlement, Application]],
) -> None:
    """Every generated module tag resolves; a new catalog action or module shows up here."""
    module_tags = [
        f
        for ent, _ in world_entitlements
        for f in ent.functions
        if len(f.split(".")) == 3 and f not in KNOWN_FUNCTIONS
    ]
    assert len(module_tags) > 500
    unresolved = sorted({t for t in module_tags if function_phrase(t) is None})
    assert not unresolved, unresolved[:20]


def test_read_catalog_entitlements_never_described_as_changing_anything(
    world_entitlements: list[tuple[Entitlement, Application]],
) -> None:
    """Outside the names and the object noun phrase (catalog data such as "change calendar
    entries" or "managed databases"), a read entitlement's text has no change wording."""
    for index, (ent, app) in enumerate(world_entitlements):
        if ent.privilege.value != "read":
            continue
        rng = random.Random(index)
        for _ in range(4):
            text = describe_entitlement(
                rng,
                name=ent.name,
                app_name=app.name,
                privilege="read",
                privileged=ent.privileged,
                data_classification=ent.data_classification.value,
                functions=ent.functions,
            )
            for value in (
                ent.name,
                app.name,
                *(p.object for p in (function_phrase(f) for f in ent.functions) if p is not None),
            ):
                text = text.replace(value, "")
            assert not CHANGE_WORDS.search(text), (ent.id, text)


# ---------------------------------------------------------------- approvers (SoD approve sides)

EDIT_CLAIM = re.compile(
    r"creat|updat|modif|\benter\b|\badd\b|\bedit|make changes|write access to", re.IGNORECASE
)
"""Wording that says the holder creates or edits something. On an approver it describes the
other side of the SoD pair ("Users can add and change journal entries" on gl.je.approve)."""


def _without_catalog_values(text: str, name: str, app_name: str, functions: tuple[str, ...]) -> str:
    """The text minus the names and object noun phrases, which are catalog data that may hold
    such words ("change requests", "Expense Report Imports Approver")."""
    for value in (
        name,
        app_name,
        *(p.object for p in (function_phrase(f) for f in functions) if p is not None),
    ):
        text = text.replace(value, "")
    return text


def test_approving_functions_are_exactly_the_approve_phrases() -> None:
    for tag, (phrase, _) in KNOWN_FUNCTIONS.items():
        reading = function_phrase(tag)
        assert reading is not None
        assert reading.approves == phrase.startswith("approve "), tag
        assert reading.approves == (tag in APPROVING_FUNCTIONS), tag
    for tag, approves in (
        ("gl.reconciliations.approve", True),
        ("gl.reconciliations.edit", False),
        ("gl.reconciliations.view", False),
        ("gl.reconciliations.export", False),
    ):
        reading = function_phrase(tag)
        assert reading is not None and reading.approves == approves, tag


def _approver_cases(
    world_entitlements: list[tuple[Entitlement, Application]],
) -> list[tuple[str, str, tuple[str, ...]]]:
    """``(name, app name, functions)`` for every approve-tagged catalog entitlement, plus a
    synthetic module approver for every module of every app (the enterprise world picks only
    some modules)."""
    cases = [
        (ent.name, app.name, ent.functions)
        for ent, app in world_entitlements
        if ent.privilege.value == "write"
        and any(p.approves for p in (function_phrase(f) for f in ent.functions) if p is not None)
    ]
    cases += [
        (f"{phrase.title()} Approver", spec.name, (f"{spec.prefix}.{slug}.approve",))
        for spec in APP_SPECS
        for slug, phrase in spec.modules
    ]
    return cases


def test_approvers_are_never_described_as_editing(
    world_entitlements: list[tuple[Entitlement, Application]],
) -> None:
    """The JE, payroll-run and vendor-payment approvers, the request and change approvers and
    every enterprise ``<module>.approve`` entitlement read as approving, never as the editing
    side of the conflict they sit on."""
    cases = _approver_cases(world_entitlements)
    hand_written = {f for _, _, functions in cases for f in functions} & APPROVING_FUNCTIONS
    assert hand_written == APPROVING_FUNCTIONS
    assert len(cases) > 1000
    for index, (name, app_name, functions) in enumerate(cases):
        rng = random.Random(index)
        for _ in range(12):
            text = describe_entitlement(
                rng,
                name=name,
                app_name=app_name,
                privilege="write",
                privileged=False,
                data_classification="internal",
                functions=functions,
            )
            stripped = _without_catalog_values(text, name, app_name, functions)
            assert not EDIT_CLAIM.search(stripped), (functions, text)


@pytest.mark.parametrize(
    ("name", "app_name", "tag", "object_sentence"),
    [
        ("Journal Entry Approver", "General Ledger", "gl.je.approve", "journal entries in General"),
        ("Payroll Run Approver", "Payroll", "payroll.approve", "payroll runs in Payroll"),
        ("Payment File Release", "Treasury and Banking", "ap.payment.approve", "vendor payments"),
        (
            "Account Reconciliations Approver",
            "General Ledger",
            "gl.reconciliations.approve",
            "account reconciliations in General Ledger",
        ),
    ],
)
def test_approver_core_sentence_names_what_is_approved(
    name: str, app_name: str, tag: str, object_sentence: str
) -> None:
    """Approvers still get a specific core sentence, worded as approval."""
    rng = random.Random(tag)
    cores = [
        describe_entitlement(
            rng,
            name=name,
            app_name=app_name,
            privilege="write",
            privileged=False,
            data_classification="confidential",
            functions=(tag,),
        ).split(". ")[0]
        for _ in range(200)
    ]
    named = [c for c in cores if object_sentence in c]
    assert len(named) > 60, cores[:10]
    assert all(re.search(r"approv", c, re.IGNORECASE) for c in named), named


# ---------------------------------------------------------------- repeated words


@pytest.mark.parametrize(
    ("template", "obj", "stutters"),
    [
        ("Users can add and change {object} in {app}.", "change requests", True),
        ("Write access to {object} in {app}.", "access requests", True),
        ("Write access to {object} in {app}.", "access profile definitions", True),
        ("Admin access to manage {app} settings, users and {object}.", "user accounts", True),
        ("Admin access to manage {app} settings, users and {object}.", "enrollment settings", True),
        ("Lets users browse and report on {object} in {app}.", "CRM reports", True),
        ("Lets users browse and report on {object} in {app}.", "on-call schedules", True),
        ("Approval rights for {object} in {app}.", "approval routing rules", True),
        ("Write access to {object} in {app}.", "journal entries", False),
        ("Admin access to manage {app} settings, users and {object}.", "vendor contracts", False),
        ("Allows creating and updating {object} in {app}.", "terms and conditions", False),
        # Only the text since the previous slot counts: "{name}" is not the word "name".
        ("{name}: full administration of {object} in {app}.", "name servers", False),
    ],
)
def test_stutter_check(template: str, obj: str, stutters: bool) -> None:
    assert descriptions._stutters(template, obj) == stutters


@pytest.mark.parametrize(
    ("template", "app_name", "stutters"),
    [
        ("View-only access to {app}.", "Access Governance", True),
        ("Admin-level access in {app}.", "Remote Access VPN", True),
        ("Grants read access in {app} for looking up {object}.", "Access Governance", True),
        ("{name}: read permissions for {app}.", "Access Governance", False),
        ("Read access to {object} in {app}.", "Access Governance", False),
        ("View-only access to {app}.", "General Ledger", False),
    ],
)
def test_stutter_check_on_the_app_name(template: str, app_name: str, stutters: bool) -> None:
    assert descriptions._stutters(template, app_name, "app") == stutters


def test_object_never_repeats_a_word_just_before_it(
    world_entitlements: list[tuple[Entitlement, Application]],
) -> None:
    """No "users and user accounts" (IdP admins), "access to access requests" or "access to
    Access Governance"."""
    clumsy = re.compile(
        r"\busers and user\b|\baccess (?:to|in) (?:the )?access\b|\bon on\b", re.IGNORECASE
    )
    for index, (ent, app) in enumerate(world_entitlements):
        rng = random.Random(index)
        for _ in range(4):
            text = describe_entitlement(
                rng,
                name=ent.name,
                app_name=app.name,
                privilege=ent.privilege.value,
                privileged=ent.privileged,
                data_classification=ent.data_classification.value,
                functions=ent.functions,
            )
            assert not clumsy.search(text), (ent.id, text)
    rng = random.Random(9)
    for name, app_name, tag in (
        ("IdP User Administrator", "Identity Provider (SSO)", "idp.admin"),
        ("Access Request Approver", "Access Governance", "iga.approve"),
        ("Profiles Editor", "Access Governance", "iga.access_profiles.edit"),
    ):
        for level in ("read", "write", "admin"):
            for _ in range(200):
                text = describe_entitlement(
                    rng,
                    name=name,
                    app_name=app_name,
                    privilege=level,
                    privileged=level == "admin",
                    data_classification="restricted",
                    functions=(tag,),
                )
                assert not clumsy.search(text), text
