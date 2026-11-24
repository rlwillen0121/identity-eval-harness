//! Cohort samples and protected family witnesses beyond the fixed personas.

use crate::archetypes::FamilyRegimeSpecV52;

pub(super) const FIXED_PERSONA_COUNT: usize = 43;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum FillRole {
    ActiveFte(&'static str),
    ActiveClinical(bool),
    ActiveAcademic(bool),
    ActiveStudent(&'static str),
    ActiveVms(&'static str, bool),
    NonActiveTerm(&'static str),
    NonActiveLoa(&'static str),
}

/// Minimum additional rows for Appendix C and the §4.2 denominator controls.
/// The fixed personas are always included and supply the remaining family and regime cases.
pub(super) struct PopulationWitnessSet {
    pub(super) cohort_rows: Vec<FillRole>,
}

impl PopulationWitnessSet {
    pub(super) const fn person_count(&self) -> usize {
        FIXED_PERSONA_COUNT + self.cohort_rows.len()
    }
}

pub(super) fn population_witnesses(spec: &FamilyRegimeSpecV52) -> PopulationWitnessSet {
    use FillRole::{
        ActiveAcademic, ActiveClinical, ActiveFte, ActiveStudent, ActiveVms, NonActiveLoa,
        NonActiveTerm,
    };

    let mut rows = vec![
        ActiveFte("Engineering"),
        ActiveFte("Finance"),
        ActiveFte("Information Technology"),
        ActiveClinical(true),
        ActiveClinical(false),
        ActiveAcademic(true),
        ActiveAcademic(false),
        ActiveStudent("Academic Affairs"),
    ];
    match spec.family_id {
        "F01" => rows.push(NonActiveTerm("Academic Affairs")),
        "F02" => rows.extend([NonActiveTerm("Finance"), NonActiveLoa("Finance")]),
        "F07" => rows.push(ActiveVms("Finance", true)),
        _ => {}
    }
    if spec.family_id == "F11" {
        // Two flawed inputs out of eight enabled Legal accounts, including two fixed personas.
        rows.extend([ActiveFte("Legal"); 6]);
    } else {
        rows.push(ActiveFte("Legal"));
    }
    if matches!(spec.family_id, "F05" | "F12") {
        rows.push(NonActiveLoa("Information Technology"));
    }
    PopulationWitnessSet { cohort_rows: rows }
}
