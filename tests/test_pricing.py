from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from identitybench.pricing import (
    PRICING_ENV,
    SOURCE_PRICING_PATH,
    PricingTable,
    cost_usd,
    default_pricing_path,
    load_pricing,
)
from identitybench.providers.base import Usage

M = 1_000_000


def test_repo_pricing_file_rows() -> None:
    table = load_pricing(SOURCE_PRICING_PATH)
    # model: (input, output, cache read, cache write 5m, cache write 1h, as_of, source prefix)
    anthropic = (date(2026, 10, 6), "Anthropic API pricing")
    catalog = (date(2026, 10, 12), "OpenRouter model catalog")
    catalog_oct14 = (date(2026, 10, 14), "OpenRouter model catalog")
    catalog_oct15 = (date(2026, 10, 15), "OpenRouter model catalog")
    expected = {
        "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5, 20.0, *anthropic),
        "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0, 8.0, *catalog),
        "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5, 4.0, *catalog),
        "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125, 0.20, *catalog),
        "claude-opus-5": (5.0, 25.0, 0.50, 6.25, 10.0, *anthropic),
        "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5, 4.0, *anthropic),
        "gpt-6-astra": (10.0, 50.0, 1.00, 12.5, 20.0, *catalog),
        "gpt-6.1-sol": (2.0, 10.0, 0.10, 2.5, 4.0, *catalog),
        "gpt-6-luna": (0.10, 0.50, 0.01, 0.125, 0.20, *catalog),
        "gemini-4-argon": (
            4.0,
            20.0,
            0.20,
            5.0,
            8.0,
            date(2026, 10, 14),
            "Google Gemini API pricing",
        ),
        "grok-4.7": (2.0, 6.0, 0.50, 2.0 * 1.25, 2.0 * 2.0, *catalog_oct14),
        "glm-5.3-flash": (0.036, 0.90, 0.01125, 0.036 * 1.25, 0.036 * 2.0, *catalog_oct14),
        "muse-spark-1.3-contributor": (1.25, 4.25, 0.15, 1.25 * 1.25, 1.25 * 2.0, *catalog_oct15),
        "kimi-k3": (0.45, 15.0, 0.28, 0.45 * 1.25, 0.45 * 2.0, *catalog_oct15),
    }
    assert set(table.models) == set(expected)
    for model, (inp, out, read, write_5m, write_1h, as_of, source) in expected.items():
        price = table.models[model]
        assert (price.input, price.output) == (inp, out)
        assert price.cache_read_rate == pytest.approx(read)
        assert price.cache_write_rate("5m") == pytest.approx(write_5m)
        assert price.cache_write_rate("1h") == pytest.approx(write_1h)
        assert price.as_of == as_of
        assert price.source.startswith(source)
    # The catalog rows name the catalog entry they were taken from.
    assert "anthropic/claude-sonnet-5.5" in table.models["claude-sonnet-5-5"].source
    assert "openai/gpt-6.1-sol" in table.models["gpt-6.1-sol"].source


def test_repo_long_context_tiers() -> None:
    table = load_pricing(SOURCE_PRICING_PATH)
    haiku = table.models["claude-haiku-5-5"].long_context
    assert haiku is not None and haiku.threshold_tokens == 100_000
    assert (haiku.input, haiku.output, haiku.cache_read_rate) == (0.50, 2.50, 0.05)
    assert haiku.cache_write_rate("1h") == pytest.approx(1.00)
    for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"):
        tier = table.models[model].long_context
        assert tier is not None and tier.threshold_tokens == 272_000
        assert tier.input == pytest.approx(2 * table.models[model].input)
    grok = table.models["grok-4.7"].long_context
    assert grok is not None and grok.threshold_tokens == 200_000
    assert (grok.input, grok.output, grok.cache_read_rate) == (4.00, 12.00, 1.00)


def test_plain_input_output_cost() -> None:
    usage = Usage(input_tokens=M, output_tokens=M)
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(24.0)
    assert cost_usd("claude-sonnet-5-5", Usage(input_tokens=1000, output_tokens=500)) == (
        pytest.approx((1000 * 2 + 500 * 10) / M)
    )


def test_cache_tokens_cost() -> None:
    usage = Usage(
        input_tokens=1000,
        output_tokens=2000,
        cache_read_tokens=50_000,
        cache_write_tokens=4000,
        reasoning_tokens=1500,
    )
    expected = (1000 * 4 + 2000 * 20 + 50_000 * 0.20 + 4000 * 5.0) / M
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(expected)
    expected_1h = (1000 * 4 + 2000 * 20 + 50_000 * 0.20 + 4000 * 8.0) / M
    assert cost_usd("claude-opus-5-5", usage, cache_ttl="1h") == pytest.approx(expected_1h)


def test_reasoning_tokens_not_double_counted() -> None:
    a = Usage(output_tokens=1000)
    b = Usage(output_tokens=1000, reasoning_tokens=800)
    assert cost_usd("claude-opus-5-5", a) == cost_usd("claude-opus-5-5", b)


def test_haiku_long_context_tier() -> None:
    short = Usage(input_tokens=100_000, output_tokens=1000)
    assert cost_usd("claude-haiku-5-5", short) == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / M)
    long = Usage(input_tokens=100_001, output_tokens=1000)
    assert cost_usd("claude-haiku-5-5", long) == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / M)


def test_long_context_threshold_counts_cached_prompt_tokens() -> None:
    usage = Usage(
        input_tokens=10_000, cache_read_tokens=80_000, cache_write_tokens=20_000, output_tokens=100
    )
    # 110K prompt tokens -> long tier for everything; cache rates default off the tier input.
    expected = (10_000 * 0.50 + 80_000 * 0.05 + 20_000 * 0.625 + 100 * 2.50) / M
    assert cost_usd("claude-haiku-5-5", usage) == pytest.approx(expected)


def test_unknown_model_has_no_price() -> None:
    assert cost_usd("qwen3-32b", Usage(input_tokens=1000)) is None


def test_custom_table_with_explicit_rates(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(
        "models:\n"
        "  m:\n"
        "    input: 1\n    output: 2\n    cache_read: 0.5\n"
        "    cache_write_5m: 1.5\n    cache_write_1h: 3\n"
        "    long_context: {threshold_tokens: 10, input: 10, output: 20, cache_read: 1}\n"
        "    as_of: 2026-01-01\n    source: test\n",
        encoding="utf-8",
    )
    table = load_pricing(path)
    usage = Usage(input_tokens=2, output_tokens=1, cache_read_tokens=2, cache_write_tokens=2)
    assert cost_usd("m", usage, table=table) == pytest.approx((2 + 2 + 1 + 3) / M)
    assert cost_usd("m", usage, table=table, cache_ttl="1h") == pytest.approx((2 + 2 + 1 + 6) / M)
    big = Usage(input_tokens=11, output_tokens=1)
    assert cost_usd("m", big, table=table) == pytest.approx((110 + 20) / M)


def test_loaded_table_keeps_its_provenance(tmp_path: Path) -> None:
    table = load_pricing(SOURCE_PRICING_PATH)
    assert table.source_path == SOURCE_PRICING_PATH
    assert table.sha256 == hashlib.sha256(SOURCE_PRICING_PATH.read_bytes()).hexdigest()
    # Same rows, different bytes (a comment): a different file, so a different sha256.
    copy = tmp_path / "pricing.yaml"
    copy.write_bytes(SOURCE_PRICING_PATH.read_bytes() + b"# edited\n")
    assert load_pricing(copy).models == table.models
    assert load_pricing(copy).sha256 != table.sha256
    # An in-memory table has no file, and a content hash that is stable.
    memory = PricingTable.model_validate(table.model_dump())
    assert memory.source_path is None
    assert memory.sha256 == PricingTable.model_validate(table.model_dump()).sha256
    assert len(memory.sha256) == 64


def test_pricing_rejects_bad_rows() -> None:
    with pytest.raises(ValueError):
        PricingTable.model_validate({"models": {"m": {"input": 1, "output": 2}}})
    with pytest.raises(ValueError):
        PricingTable.model_validate(
            {"models": {"m": {"input": -1, "output": 2, "as_of": "2026-01-01", "source": "s"}}}
        )


def test_pricing_path_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRICING_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    # No ./pricing.yaml: fall back to the source checkout's file.
    assert default_pricing_path() == SOURCE_PRICING_PATH
    local = tmp_path / "pricing.yaml"
    local.write_text(
        "models:\n  m: {input: 1, output: 2, as_of: 2026-01-01, source: t}\n", encoding="utf-8"
    )
    assert default_pricing_path() == local
    assert set(load_pricing().models) == {"m"}
    override = tmp_path / "other.yaml"
    monkeypatch.setenv(PRICING_ENV, str(override))
    assert default_pricing_path() == override


def test_pricing_path_missing_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRICING_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("identitybench.pricing.SOURCE_PRICING_PATH", tmp_path / "nope.yaml")
    with pytest.raises(FileNotFoundError, match=PRICING_ENV):
        default_pricing_path()
