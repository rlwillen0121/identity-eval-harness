"""Markdown escaping: untrusted text can't become markup or break out of a code block."""

from __future__ import annotations

from identitybench.report.markdown_text import escape_cell, escape_inline, fenced


def test_markup_characters_are_escaped() -> None:
    text = escape_inline("<script>x</script> | **bold** `code` [a](b) ~~s~~")
    assert "<" not in text.replace("\\<", "")
    assert ">" not in text.replace("\\>", "")
    assert "|" not in text.replace("\\|", "")
    assert "**" not in text.replace("\\*", "")
    assert "`" not in text.replace("\\`", "")
    assert "[" not in text.replace("\\[", "")


def test_identifiers_with_underscores_stay_readable() -> None:
    assert escape_inline("always_escalate") == "always_escalate"
    assert escape_inline("_emphasis_") == "\\_emphasis\\_"


def test_line_breaks_fold_and_entities_are_neutralized() -> None:
    assert escape_inline("a\nb\r\n  c") == "a b c"
    assert escape_inline("&lt;b&gt; & co") == "&amp;lt;b&amp;gt; & co"


def test_an_empty_cell_still_fills_its_column() -> None:
    assert escape_cell("") == " "


def test_a_fence_is_longer_than_any_backtick_run_inside() -> None:
    block = fenced("before\n````\nafter")
    first, *_, last = block.split("\n")
    assert first == "`````text"
    assert last == "`````"
    assert "before\n````\nafter" in block
    assert fenced("plain").split("\n")[0] == "```text"
