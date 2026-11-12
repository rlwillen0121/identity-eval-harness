"""The active chart helpers and icon assets do not depend on archived reports."""

from defusedxml import ElementTree as ET

from identitybench.report.chart_style import Svg, draw_icon, shortfall_bin


def test_style_embeds_packaged_icon_and_escapes_text():
    svg = Svg("<title>", "description & detail", "example", 200)
    draw_icon(svg, "gpt-example", 100)
    svg.text(200, 100, "<script>example</script>", 16)
    output = svg.output()
    assert "data:image/png;base64," in output
    assert "<script>" not in output
    assert "&lt;title&gt;" in output
    assert ET.fromstring(output).tag == "{http://www.w3.org/2000/svg}svg"


def test_shortfall_boundaries():
    assert [shortfall_bin(p) for p in (0, 0.1, 1, 1.1, 2.5, 5, 10, 25, 25.1)] == [
        0,
        1,
        1,
        2,
        2,
        3,
        4,
        5,
        6,
    ]
