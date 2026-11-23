//! Entry-wide failure diagnostics (SPEC-v5.2 §10.2–§10.3).

use std::collections::BTreeMap;
use std::fmt::Write;

use ib_v52_schemas::{CategoryIdV52, EpisodeExecutionRecordV52};
use serde::Serialize;

const MIN_PRIVATE_ENGAGEMENTS: usize = 3;
const TOP_CHECKS: usize = 10;

#[derive(Serialize)]
pub(crate) struct FailureTaxonomyV52<'a> {
    by_category: BTreeMap<CategoryIdV52, CategoryFailures<'a>>,
    termination_counts: BTreeMap<&'a str, usize>,
    suppressed_cells: usize,
}

#[derive(Default, Serialize)]
struct CategoryFailures<'a> {
    check_counts: Vec<CheckCount<'a>>,
    top_checks: Vec<&'a str>,
}

#[derive(Serialize)]
struct CheckCount<'a> {
    check_name: &'a str,
    count: usize,
}

#[derive(Default)]
struct Cell<'a> {
    count: usize,
    // Retain only enough distinct IDs to decide whether the cell can be published.
    engagements: Vec<&'a str>,
}

impl<'a> FailureTaxonomyV52<'a> {
    pub(crate) fn from_records(records: &'a [EpisodeExecutionRecordV52], private: bool) -> Self {
        let mut cells = BTreeMap::<(CategoryIdV52, &str), Cell<'_>>::new();
        let mut termination_counts = BTreeMap::new();
        for record in records {
            *termination_counts
                .entry(record.termination_reason.as_str())
                .or_default() += 1;
            for failure in record.score.iter().flat_map(|score| &score.failures) {
                let cell = cells
                    .entry((failure.category, failure.check_name.as_str()))
                    .or_default();
                cell.count += 1;
                let id = record.episode_id.as_str();
                if private
                    && cell.engagements.len() < MIN_PRIVATE_ENGAGEMENTS
                    && !cell.engagements.contains(&id)
                {
                    cell.engagements.push(id);
                }
            }
        }

        let mut by_category = BTreeMap::<CategoryIdV52, CategoryFailures<'_>>::new();
        let mut suppressed_cells = 0;
        for ((category, check_name), cell) in cells {
            if private && cell.engagements.len() < MIN_PRIVATE_ENGAGEMENTS {
                suppressed_cells += 1;
                continue;
            }
            by_category
                .entry(category)
                .or_default()
                .check_counts
                .push(CheckCount {
                    check_name,
                    count: cell.count,
                });
        }
        for category in by_category.values_mut() {
            category.check_counts.sort_unstable_by(|a, b| {
                b.count
                    .cmp(&a.count)
                    .then_with(|| a.check_name.cmp(b.check_name))
            });
            category.top_checks = category
                .check_counts
                .iter()
                .take(TOP_CHECKS)
                .map(|check| check.check_name)
                .collect();
        }
        Self {
            by_category,
            termination_counts,
            suppressed_cells,
        }
    }

    pub(crate) fn append_markdown(&self, md: &mut String) {
        md.push_str(
            "\n## Failure Taxonomy (SPEC-v5.2 §10.2–§10.3)\n\n\
             | Category | Check | Failures | Top-10 rank in category |\n\
             | :--- | :--- | ---: | ---: |\n",
        );
        if self.by_category.is_empty() {
            md.push_str("| — | — | — | — |\n");
        }
        for (category, failures) in &self.by_category {
            for (index, check) in failures.check_counts.iter().enumerate() {
                let rank = if index < TOP_CHECKS {
                    (index + 1).to_string()
                } else {
                    "—".to_owned()
                };
                writeln!(
                    md,
                    "| `{}` | `{}` | {} | {rank} |",
                    category.as_str(),
                    markdown_label(check.check_name),
                    check.count,
                )
                .expect("writing to a string");
            }
        }
        writeln!(
            md,
            "\nSuppressed category/check cells: {}.\n\n\
             | Termination reason | Slots |\n\
             | :--- | ---: |",
            self.suppressed_cells
        )
        .expect("writing to a string");
        for (termination, count) in &self.termination_counts {
            writeln!(md, "| `{}` | {count} |", markdown_label(termination))
                .expect("writing to a string");
        }
    }
}

fn markdown_label(label: &str) -> String {
    label.replace(['\r', '\n'], " ").replace('|', "\\|")
}
