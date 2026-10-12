"""What a published report shows for each configuration's model and endpoint."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from identitybench.report.provenance import SELF_HOSTED_ENDPOINT, _published_model


def _manifest(**modes: str) -> Any:
    cost = {k: SimpleNamespace(cost_mode=v) for k, v in modes.items()}
    return cast(Any, SimpleNamespace(cost=cost))


def test_a_self_hosted_endpoint_shows_its_label_and_no_identity_hash() -> None:
    manifest = _manifest(glm="unmetered")
    shown = _published_model(manifest, "glm", "glm-5.3-flash", "route/glm-5.3-flash", "d098535d")
    assert shown == ("glm-5.3-flash", SELF_HOSTED_ENDPOINT)


def test_gateways_vendors_and_clis_keep_their_public_names_and_hashes() -> None:
    manifest = _manifest(router="reported", cli="reported", api="metered")
    for config_id in ("router", "cli", "api"):
        assert _published_model(manifest, config_id, "label", "vendor/model", "abc123") == (
            "vendor/model",
            "abc123",
        )


def test_an_unlabelled_or_uncosted_configuration_is_unchanged() -> None:
    assert _published_model(_manifest(x="unmetered"), "x", "same", "same", "h") == ("same", "h")
    assert _published_model(_manifest(), "missing", "label", "model", "h") == ("model", "h")
