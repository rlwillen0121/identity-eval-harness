//! Declarative attribute transform AST for IdentityBench v5.1 (modeled on SailPoint ISC Transforms).

use indexmap::IndexMap;
use serde::{Deserialize, Serialize};

/// Top-level `config/transforms.json` document authored by the practitioner agent.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TransformsConfigV51 {
    pub schema_version: String,
    pub transforms: IndexMap<String, TransformExprV51>,
}

impl Default for TransformsConfigV51 {
    fn default() -> Self {
        Self {
            schema_version: "v5.1".to_owned(),
            transforms: IndexMap::new(),
        }
    }
}

/// Comparison operator for `conditional` transforms.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CompareOpV51 {
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
/// `"CUTOVER_DATE"` resolves to the engagement's cutover date).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DateCompareOpV51 {
    Lt,
    Le,
    Gt,
    Ge,
    Eq,
}

/// Declarative transform expression (`op`-tagged JSON object).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum TransformExprV51 {
    /// Read a raw attribute from a correlated source record (`source_id`, `field`).
    SourceAttr { source_id: String, field: String },
    /// Return a constant string (or empty string `""` to represent null/blank).
    Literal { value: String },
    /// Reference another named transform in `config/transforms.json` (acyclic).
    RefTransform { name: String },
    /// Evaluate `values` in order and return the first non-null (and non-blank when `ignore_blank` is true) value.
    FirstValid {
        values: Vec<TransformExprV51>,
        #[serde(default = "default_true")]
        ignore_blank: bool,
    },
    /// Map `input` through an exact key->value dictionary, falling back to `default_value` (or `""` if omitted).
    Lookup {
        input: Box<TransformExprV51>,
        table: IndexMap<String, String>,
        #[serde(default)]
        default_value: Option<Box<TransformExprV51>>,
    },
    /// Concatenate evaluated string `parts`.
    Concat { parts: Vec<TransformExprV51> },
    /// Trim leading and trailing whitespace.
    Trim { input: Box<TransformExprV51> },
    /// Convert ASCII/Unicode letters to lowercase.
    Lower { input: Box<TransformExprV51> },
    /// Convert ASCII/Unicode letters to uppercase.
    Upper { input: Box<TransformExprV51> },
    /// Transliterate accented/diacritic characters to ASCII (`José Núñez` -> `Jose Nunez`, `Müller` -> `Muller`, `Ł` -> `L`).
    StripDiacritics { input: Box<TransformExprV51> },
    /// Keep only characters matching an allowed policy or replace regex matches with `replacement`.
    RegexReplace {
        input: Box<TransformExprV51>,
        pattern: String,
        replacement: String,
    },
    /// Character-indexed (Unicode scalar, 0-based) substring of at most `max_len` characters.
    Substring {
        input: Box<TransformExprV51>,
        start: usize,
        max_len: usize,
    },
    /// Normalize full country names or ISO-3 codes (`"United States"`, `"USA"`, `"Germany"`, `"DEU"`, etc.) to 2-char ISO-3166-1 alpha-2 (`"US"`, `"DE"`).
    #[serde(alias = "iso_country_2")]
    IsoCountry2 { input: Box<TransformExprV51> },
    /// Conditional branch comparing `left` and `right` (for `in_list`, `right` is pipe-delimited `"A|B|C"`).
    Conditional {
        left: Box<TransformExprV51>,
        cmp: CompareOpV51,
        #[serde(default)]
        right: Option<Box<TransformExprV51>>,
        if_true: Box<TransformExprV51>,
        if_false: Box<TransformExprV51>,
    },
    /// Date comparison (`YYYY-MM-DD` or epoch seconds, with `"CUTOVER_DATE"` keyword support).
    DateCompare {
        left: Box<TransformExprV51>,
        cmp: DateCompareOpV51,
        right: Box<TransformExprV51>,
        if_true: Box<TransformExprV51>,
        if_false: Box<TransformExprV51>,
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
        let canonical: TransformExprV51 =
            serde_json::from_str(r#"{"op":"iso_country2","input":{"op":"literal","value":"USA"}}"#)
                .expect("canonical iso_country2");
        let aliased: TransformExprV51 = serde_json::from_str(
            r#"{"op":"iso_country_2","input":{"op":"literal","value":"USA"}}"#,
        )
        .expect("aliased iso_country_2");
        assert_eq!(canonical, aliased);
        let serialized = serde_json::to_string(&aliased).expect("serialize");
        assert!(serialized.contains(r#""op":"iso_country2""#));
    }
}
