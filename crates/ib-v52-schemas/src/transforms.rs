//! Declarative attribute transform AST for IdentityBench v5.2 (modeled on SailPoint ISC Transforms).

use indexmap::IndexMap;
use serde::{Deserialize, Serialize};

/// Top-level `config/transforms.json` document authored by the practitioner agent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TransformsConfigV52 {
    pub schema_version: String,
    pub transforms: IndexMap<String, TransformExprV52>,
}

impl Default for TransformsConfigV52 {
    fn default() -> Self {
        Self {
            schema_version: "v5.2".to_owned(),
            transforms: IndexMap::new(),
        }
    }
}

/// Comparison operator for `conditional` transforms.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CompareOpV52 {
    Eq,
    Ne,
    Contains,
    StartsWith,
    EndsWith,
    InList,
    IsEmpty,
    IsNotEmpty,
}

/// Comparison operator for `date_compare` transforms (supports `YYYY-MM-DD` and integer epoch seconds;
/// `"CUTOVER_DATE"` resolves to the scenario's cutover date).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DateCompareOpV52 {
    Lt,
    Le,
    Gt,
    Ge,
    Eq,
}

/// Declarative transform expression (`op`-tagged JSON object).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum TransformExprV52 {
    /// Read a raw attribute from a correlated source record (`source_id`, `field`).
    SourceAttr { source_id: String, field: String },
    /// Return a constant string (or empty string `""` to represent null/blank).
    Literal { value: String },
    /// Reference another named transform in `config/transforms.json` (acyclic).
    RefTransform { name: String },
    /// Evaluate `values` in order and return the first non-null (and non-blank when `ignore_blank` is true) value.
    FirstValid {
        values: Vec<TransformExprV52>,
        #[serde(default = "default_true")]
        ignore_blank: bool,
    },
    /// Map `input` through an exact key->value dictionary, falling back to `default_value` (or `""` if omitted).
    Lookup {
        input: Box<TransformExprV52>,
        table: IndexMap<String, String>,
        #[serde(default)]
        default_value: Option<Box<TransformExprV52>>,
    },
    /// Concatenate evaluated string `parts`.
    Concat { parts: Vec<TransformExprV52> },
    /// Trim leading and trailing whitespace.
    Trim { input: Box<TransformExprV52> },
    /// Convert ASCII/Unicode letters to lowercase.
    Lower { input: Box<TransformExprV52> },
    /// Convert ASCII/Unicode letters to uppercase.
    Upper { input: Box<TransformExprV52> },
    /// Transliterate accented/diacritic characters to ASCII (`José Núñez` -> `Jose Nunez`, `Müller` -> `Muller`, `Ł` -> `L`).
    StripDiacritics { input: Box<TransformExprV52> },
    /// Keep only characters matching an allowed policy or replace regex matches with `replacement`.
    RegexReplace {
        input: Box<TransformExprV52>,
        pattern: String,
        replacement: String,
    },
    /// Character-indexed (Unicode scalar, 0-based) substring of at most `max_len` characters.
    Substring {
        input: Box<TransformExprV52>,
        start: usize,
        max_len: usize,
    },
    /// Normalize full country names or ISO-3 codes (`"United States"`, `"USA"`, `"Germany"`, `"DEU"`, etc.) to 2-char ISO-3166-1 alpha-2 (`"US"`, `"DE"`).
    #[serde(alias = "iso_country_2")]
    IsoCountry2 { input: Box<TransformExprV52> },
    /// Conditional branch comparing `left` and `right` (for `in_list`, `right` is pipe-delimited `"A|B|C"`).
    Conditional {
        left: Box<TransformExprV52>,
        cmp: CompareOpV52,
        #[serde(default)]
        right: Option<Box<TransformExprV52>>,
        if_true: Box<TransformExprV52>,
        if_false: Box<TransformExprV52>,
    },
    /// Date comparison (`YYYY-MM-DD` or epoch seconds, with `"CUTOVER_DATE"` keyword support).
    DateCompare {
        left: Box<TransformExprV52>,
        cmp: DateCompareOpV52,
        right: Box<TransformExprV52>,
        if_true: Box<TransformExprV52>,
        if_false: Box<TransformExprV52>,
    },
}

const fn default_true() -> bool {
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_country2_accepts_canonical_and_underscored_alias() {
        let canonical: TransformExprV52 =
            serde_json::from_str(r#"{"op":"iso_country2","input":{"op":"literal","value":"USA"}}"#)
                .expect("canonical iso_country2");
        let aliased: TransformExprV52 = serde_json::from_str(
            r#"{"op":"iso_country_2","input":{"op":"literal","value":"USA"}}"#,
        )
        .expect("aliased iso_country_2");
        assert_eq!(canonical, aliased);
        let serialized = serde_json::to_string(&aliased).expect("serialize");
        assert!(serialized.contains(r#""op":"iso_country2""#));
    }
}
