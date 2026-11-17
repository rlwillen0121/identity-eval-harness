use ib_v5_report::{code_cell, escape_cell, escape_inline, fenced_text};

#[test]
fn markup_characters_are_escaped() {
    let text = escape_inline("<script>x</script> | **bold** `code` [a](b) ~~s~~");
    assert!(!text.replace("\\<", "").contains('<'));
    assert!(!text.replace("\\>", "").contains('>'));
    assert!(!text.replace("\\|", "").contains('|'));
    assert!(!text.replace("\\*", "").contains("**"));
    assert!(!text.replace("\\`", "").contains('`'));
    assert!(!text.replace("\\[", "").contains('['));
}

#[test]
fn identifiers_with_underscores_stay_readable() {
    assert_eq!(escape_inline("always_escalate"), "always_escalate");
    assert_eq!(escape_inline("_emphasis_"), "\\_emphasis\\_");
    assert_eq!(escape_inline("__"), "\\_\\_");
    assert_eq!(escape_inline("a__b"), "a__b");
}

#[test]
fn line_breaks_fold_and_entities_are_neutralized() {
    assert_eq!(escape_inline("a\nb\r\n  c"), "a b c");
    assert_eq!(escape_inline("a\x1c\x1f\t b"), "a b");
    assert_eq!(escape_inline("&lt;b&gt; & co"), "&amp;lt;b&amp;gt; & co");
    assert_eq!(escape_inline("&#123; &#x1f;"), "&amp;#123; &amp;#x1f;");
}

#[test]
fn empty_cell_still_fills_column() {
    assert_eq!(escape_cell(""), " ");
}

#[test]
fn fence_is_longer_than_any_backtick_run_inside() {
    let block = fenced_text("before\n````\nafter");
    let lines: Vec<&str> = block.split('\n').collect();
    assert_eq!(lines.first().copied(), Some("`````text"));
    assert_eq!(lines.last().copied(), Some("`````"));
    assert!(block.contains("before\n````\nafter"));
    assert_eq!(fenced_text("plain").split('\n').next(), Some("```text"));
}

#[test]
fn code_cell_cannot_be_closed_by_backticks_or_split_by_hostile_text() {
    let text = "` <img src=x onerror=alert(1)> ` | [link](https://bad.example)\nnext";
    let cell = code_cell(text);
    assert!(cell.starts_with("``") && cell.ends_with("``"));
    assert!(cell.contains("<img src=x onerror=alert(1)>"));
    assert!(cell.contains("\\|"));
    assert!(!cell.contains('\n'));
    assert!(!cell.contains("```"));
}

#[test]
fn inline_text_neutralizes_autolinks() {
    assert!(!escape_inline("https://bad.example").contains("https://"));
    assert!(!escape_inline("http://bad.example").contains("http://"));
    assert!(!escape_inline("ftp://bad.example").contains("ftp://"));
    assert!(!escape_inline("mailto:user@example.test").contains("mailto:"));
    assert!(!escape_inline("www.bad.example").contains("www."));
    assert!(!escape_inline("person@example.test").contains('@'));
}
