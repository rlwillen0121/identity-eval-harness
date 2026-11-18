use std::path::PathBuf;

use ib_v5_generator::load_v5_dataset;

use super::structural_gates;
use crate::report::GateStatusV5;

fn sealed_v5_dev() -> crate::DatasetArtifactsV5 {
    let dataset = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../datasets/v5_dev");
    load_v5_dataset(&dataset).expect("sealed v5_dev artifacts load without modification")
}

fn gate<'a>(gates: &'a [crate::GateResultV5], gate_id: &str) -> &'a crate::GateResultV5 {
    gates
        .iter()
        .find(|item| item.gate_id == gate_id)
        .unwrap_or_else(|| panic!("missing {gate_id}"))
}

#[test]
fn sealed_dev_structural_gates_pass() {
    let artifacts = sealed_v5_dev();
    let gates = structural_gates(&artifacts);
    assert_eq!(
        gate(&gates, "FIXTURE-CLOSURE-V5").status,
        GateStatusV5::Pass,
        "fixture errors: {:?}",
        gate(&gates, "FIXTURE-CLOSURE-V5").errors
    );
    assert_eq!(
        gate(&gates, "G-SLICE-EXCLUSIVITY-01").status,
        GateStatusV5::Pass
    );
    assert_eq!(gate(&gates, "G-QUERY-01").status, GateStatusV5::Pass);
    assert_eq!(
        gate(&gates, "G-ORACLE-REPLAY-V5").status,
        GateStatusV5::Pass
    );
}

#[test]
fn phase_delta_mutation_is_reported_by_fixture_closure() {
    let mut artifacts = sealed_v5_dev();
    let person_id = artifacts.oracles[0]
        .omniscient_s1
        .hr_persons
        .keys()
        .next()
        .expect("dev fixture has a person")
        .clone();
    artifacts.episodes[0]
        .phase2_delta_person_ids
        .push(person_id);

    let gates = structural_gates(&artifacts);
    let fixture = gate(&gates, "FIXTURE-CLOSURE-V5");
    assert_eq!(fixture.status, GateStatusV5::Fail);
    assert!(
        fixture
            .errors
            .iter()
            .any(|error| error.contains("phase2_delta_person_ids does not equal changed HR rows"))
    );
}

#[test]
fn missing_foreign_key_is_reported_by_fixture_closure() {
    let mut artifacts = sealed_v5_dev();
    let world = &mut artifacts.oracles[0].omniscient_s1;
    let person = world
        .hr_persons
        .values_mut()
        .next()
        .expect("dev fixture has a person");
    person.engagements[0].manager_person_id = Some("pers_missing_for_test".to_owned());
    let gates = structural_gates(&artifacts);
    let fixture = gate(&gates, "FIXTURE-CLOSURE-V5");
    assert_eq!(fixture.status, GateStatusV5::Fail);
    assert!(
        fixture
            .errors
            .iter()
            .any(|error| { error.contains("references missing pers_missing_for_test") }),
        "fixture errors: {:?}",
        fixture.errors
    );
}
