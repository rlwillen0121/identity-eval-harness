"""Config labels must remain literal text in every legacy scorecard table."""

from html.parser import HTMLParser

import pytest
from markdown_it import MarkdownIt

from identitybench.v3.report import render_v3_markdown_report
from identitybench.v3.runner import V3RunArtifact
from identitybench.v3.scoring import ModelScoreSummaryV3
from identitybench.v4.report import render_v4_markdown_report
from identitybench.v4.runner import V4RunArtifact
from identitybench.v4.schemas import RunConfigV4, SuiteScoreSummaryV4


class _Cells(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.cells: list[str] = []
        self.in_cell = False
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag == "td":
            self.in_cell = True
            self.cells.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self.in_cell = False

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cells[-1] += data


@pytest.mark.parametrize("version,occurrences", [(3, 2), (4, 3)])
@pytest.mark.parametrize(
    "label",
    ["safe_model", "x`<img src=x> [link](https://example.com)|\r\ny", "` `` \\|"],
)
def test_config_labels_cannot_escape_table_cells(
    version: int,
    occurrences: int,
    label: str,
) -> None:
    if version == 3:
        fields = {name: 0.0 for name in ModelScoreSummaryV3.model_fields}
        summary = ModelScoreSummaryV3.model_validate(
            {
                **fields,
                "config_id": label,
                "episode_count": 1,
                "scoring_status": "eligible",
                "infrastructure_invalid_count": 0,
                "by_category": {},
            }
        )
        artifact = V3RunArtifact(suite="v3_dev", episode_count=1, summaries={label: summary})
        report = render_v3_markdown_report(artifact)
    else:
        fields = {name: 0.0 for name in SuiteScoreSummaryV4.model_fields}
        summary4 = SuiteScoreSummaryV4.model_validate(
            {
                **fields,
                "suite_id": "v4_dev",
                "run_config": RunConfigV4.model_validate(
                    {
                        "topology_id": "T0_SINGLE_MONOLITH",
                        "skill_condition": "S0_NO_SKILLS",
                        "lead_model_id": "test",
                        "worker_model_id": "test",
                        "pricing_manifest_sha256": "test",
                        "run_config_sha256": "test",
                    }
                ),
                "n_episodes": 1,
            }
        )
        artifact4 = V4RunArtifact(
            suite="v4_dev",
            episode_count=1,
            wave_count=3,
            summaries={label: summary4},
        )
        report = render_v4_markdown_report(artifact4)
    parsed = _Cells()
    parsed.feed(MarkdownIt().enable("table").render(report))
    assert "img" not in parsed.tags
    assert "a" not in parsed.tags
    assert parsed.cells.count(" ".join(label.split())) == occurrences
