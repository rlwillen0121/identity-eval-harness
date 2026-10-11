"""Fixtures for the request/review policy tests: fresh scenario builders per test."""

from __future__ import annotations

import pytest

from tests.policy.builders import CaseBuilder, request_case, review_case


@pytest.fixture
def req() -> CaseBuilder:
    """A request scenario in which no rule fires (see ``builders.request_case``)."""
    return request_case()


@pytest.fixture
def rev() -> CaseBuilder:
    """A review scenario with one holder and one application (see ``builders.review_case``)."""
    return review_case()
