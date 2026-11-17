//! Schema-driven atomic mutation alternatives shared by validation proofs.

use std::sync::LazyLock;

use ib_v5_schemas::{AtomicMutationV5, StrictV5Model};
use serde_json::Value;

static MUTATION_ANNOTATION_METADATA_V5: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("mutation_annotation_v5.json"))
        .expect("checked-in AtomicMutationV5 annotation metadata must be valid JSON")
});

/// Generate ordered, strict single-field alternatives for one atomic mutation.
pub fn single_field_semantic_mutants(mutation: &AtomicMutationV5) -> Vec<AtomicMutationV5> {
    const EXCLUDED: [&str; 4] = ["mutation_kind", "target_entity_id", "if_match", "cite"];
    let Ok(Value::Object(object)) = serde_json::to_value(mutation) else {
        return Vec::new();
    };
    let Some(model_name) = mutation_model_name(mutation.mutation_kind()) else {
        return Vec::new();
    };
    let Some(fields) = model_fields(model_name) else {
        return Vec::new();
    };
    let mut mutants = Vec::new();
    for field_spec in fields {
        let Some(field) = field_spec.get("name").and_then(Value::as_str) else {
            continue;
        };
        if EXCLUDED.contains(&field) {
            continue;
        }
        let Some(annotation) = field_spec.get("annotation") else {
            continue;
        };
        let Some(value) = object.get(field) else {
            continue;
        };
        for alternative in annotation_alternatives(annotation, value) {
            let mut candidate_value = Value::Object(object.clone());
            if let Some(candidate_object) = candidate_value.as_object_mut() {
                candidate_object.insert(field.to_owned(), alternative);
            }
            let Ok(candidate) = serde_json::from_value::<AtomicMutationV5>(candidate_value) else {
                continue;
            };
            if candidate.validate().is_ok() && candidate != *mutation {
                mutants.push(candidate);
            }
        }
    }
    mutants
}

fn mutation_model_name(mutation_kind: &str) -> Option<&'static str> {
    MUTATION_ANNOTATION_METADATA_V5
        .get("variants")?
        .as_array()?
        .iter()
        .find(|variant| {
            variant.get("mutation_kind").and_then(Value::as_str) == Some(mutation_kind)
        })?
        .get("model")?
        .as_str()
}

fn model_fields(model_name: &str) -> Option<&'static Vec<Value>> {
    MUTATION_ANNOTATION_METADATA_V5
        .get("models")?
        .as_array()?
        .iter()
        .find(|model| model.get("name").and_then(Value::as_str) == Some(model_name))?
        .get("fields")?
        .as_array()
}

/// Return strict, schema-driven alternatives in the same annotation order as Python's
/// `_annotation_alternatives`. The metadata asset is generated from the concrete Pydantic
/// mutation model field annotations and keeps declaration/member order explicit.
fn annotation_alternatives(annotation: &Value, value: &Value) -> Vec<Value> {
    let Some(kind) = annotation.get("kind").and_then(Value::as_str) else {
        return Vec::new();
    };
    match kind {
        "string" => match value.as_str() {
            Some(value) => string_alternatives(value),
            None if value.is_null() => vec![Value::String("negative_value".to_owned())],
            None => Vec::new(),
        },
        "bool" => value
            .as_bool()
            .map(|value| vec![Value::Bool(!value)])
            .unwrap_or_default(),
        "int" => value
            .as_i64()
            .map(|value| vec![Value::from(value + 1), Value::from(value - 1)])
            .or_else(|| {
                value.as_u64().map(|value| {
                    let mut alternatives = vec![Value::from(value + 1)];
                    if value > 0 {
                        alternatives.push(Value::from(value - 1));
                    }
                    alternatives
                })
            })
            .unwrap_or_default(),
        "literal" => {
            let Some(values) = annotation.get("values").and_then(Value::as_array) else {
                return Vec::new();
            };
            values
                .iter()
                .filter(|candidate| *candidate != value)
                .cloned()
                .collect()
        }
        "none" => {
            if value.is_null() {
                Vec::new()
            } else {
                vec![Value::Null]
            }
        }
        "union" => annotation
            .get("members")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .flat_map(|member| annotation_alternatives(member, value))
            .collect(),
        "tuple" => tuple_alternatives(annotation, value),
        "model" => model_alternatives(annotation, value),
        _ => Vec::new(),
    }
}

fn string_alternatives(value: &str) -> Vec<Value> {
    if value.contains('@')
        && let Some((identifier, suffix)) = value.split_once('@')
        && let Some((etag, generation)) = suffix.split_once(':')
        && let Some(first) = etag.chars().next()
    {
        let replacement = if first != '0' { '0' } else { '1' };
        let mut changed_etag = String::with_capacity(etag.len());
        changed_etag.push(replacement);
        changed_etag.push_str(&etag[first.len_utf8()..]);
        return vec![Value::String(format!(
            "{identifier}@{changed_etag}:{generation}"
        ))];
    }
    if let Some((prefix, tail)) = value.split_once(':') {
        return vec![Value::String(format!("{prefix}:{tail}_negative"))];
    }
    if let Some((prefix, tail)) = value.split_once('_') {
        return vec![Value::String(format!("{prefix}_{tail}_negative"))];
    }
    vec![
        Value::String(format!("{value}_negative")),
        Value::String("negative_value".to_owned()),
    ]
}

fn tuple_alternatives(annotation: &Value, value: &Value) -> Vec<Value> {
    let Some(values) = value.as_array() else {
        return Vec::new();
    };
    let Some(items) = annotation.get("items").and_then(Value::as_array) else {
        return Vec::new();
    };
    let variadic = annotation
        .get("variadic")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if !values.is_empty() {
        let mut alternatives = vec![Value::Array(Vec::new())];
        for (index, item) in values.iter().enumerate() {
            let mut shortened = values.clone();
            shortened.remove(index);
            alternatives.push(Value::Array(shortened));
            let Some(item_annotation) = tuple_item_annotation(items, variadic, index) else {
                continue;
            };
            for replacement in annotation_alternatives(item_annotation, item) {
                let mut changed = values.clone();
                changed[index] = replacement;
                alternatives.push(Value::Array(changed));
            }
        }
        return alternatives;
    }

    let components = if variadic {
        items.first().into_iter().collect::<Vec<_>>()
    } else {
        items.iter().collect::<Vec<_>>()
    };
    let mut seed = Vec::new();
    for component in components {
        let kind = component
            .get("kind")
            .and_then(Value::as_str)
            .unwrap_or_default();
        if kind == "string" {
            seed.push(Value::String("negative_value".to_owned()));
        } else if kind == "literal" {
            if let Some(choice) = component
                .get("values")
                .and_then(Value::as_array)
                .and_then(|choices| choices.first())
            {
                seed.push(choice.clone());
            }
        } else if let Some(first) = annotation_alternatives(component, &Value::Null).first() {
            seed.push(first.clone());
        }
    }
    if seed.is_empty() {
        Vec::new()
    } else {
        vec![Value::Array(seed)]
    }
}

fn tuple_item_annotation(items: &[Value], variadic: bool, index: usize) -> Option<&Value> {
    if variadic {
        items.first()
    } else {
        items.get(index)
    }
}

fn model_alternatives(annotation: &Value, value: &Value) -> Vec<Value> {
    let Some(model_name) = annotation.get("name").and_then(Value::as_str) else {
        return Vec::new();
    };
    let Some(object) = value.as_object() else {
        return Vec::new();
    };
    let Some(fields) = model_fields(model_name) else {
        return Vec::new();
    };
    let mut alternatives = Vec::new();
    for field_spec in fields {
        let Some(field) = field_spec.get("name").and_then(Value::as_str) else {
            continue;
        };
        let Some(field_annotation) = field_spec.get("annotation") else {
            continue;
        };
        let Some(current) = object.get(field) else {
            continue;
        };
        for replacement in annotation_alternatives(field_annotation, current) {
            let mut candidate = object.clone();
            candidate.insert(field.to_owned(), replacement);
            alternatives.push(Value::Object(candidate));
        }
    }
    alternatives
}
