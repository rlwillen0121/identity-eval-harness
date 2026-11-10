"""Test-only compatibility helpers for the v3 suite."""

from __future__ import annotations

import os

import pytest

import identitybench.v3.environment as environment_module
import identitybench.v3.runner as runner_module
import identitybench.v3.sandbox as sandbox_module
from identitybench.v3.sandbox import ConnectorSandboxResult
from identitybench.v3.schemas import ConnectorTargetSpec


@pytest.fixture(autouse=True)
def _explicitly_opt_out_when_os_sandbox_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep environment tests focused on connector/IGA behavior on hosts without a backend.

    Direct sandbox tests still exercise the production default and its fail-closed behavior.
    This fixture uses the existing `os_sandbox=False` API only for environment-mediated
    synthetic connector runs when the OS-level backend is disabled or unavailable.
    """
    disabled = os.environ.get("IDENTITYBENCH_OS_SANDBOX", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }
    if not disabled and sandbox_module.os_sandbox_available():
        return

    # Most runner tests use synthetic providers and assert model-loop behavior. The dedicated
    # infrastructure regressions override this probe to exercise the fail-closed path.
    monkeypatch.setattr(runner_module, "os_sandbox_available", lambda: True)

    def execute_connector_without_os_sandbox(
        code: str, target: ConnectorTargetSpec
    ) -> ConnectorSandboxResult:
        return sandbox_module.execute_connector_in_sandbox(code, target, os_sandbox=False)

    monkeypatch.setattr(
        environment_module,
        "execute_connector_in_sandbox",
        execute_connector_without_os_sandbox,
    )
