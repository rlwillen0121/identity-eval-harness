//! Mock spoke connector endpoints per active spoke.

use ib_v52_schemas::{ConnectorKindV52, MockSpokeEndpointV52, PaginationKindV52};
use indexmap::IndexMap;

use crate::archetypes::FamilyRegimeSpecV52;

use super::types::ClientParametersV52;

pub(super) fn build_mock_endpoints(
    spec: &FamilyRegimeSpecV52,
    client: &ClientParametersV52,
) -> IndexMap<String, MockSpokeEndpointV52> {
    let mut map = IndexMap::new();

    map.insert(
        "ad".to_owned(),
        MockSpokeEndpointV52 {
            connector_id: "ad".to_owned(),
            kind: ConnectorKindV52::AdLdap,
            expected_pagination: PaginationKindV52::CursorToken,
            expected_cursor_request_param: Some(client.ad_cursor_param.clone()),
            expected_cursor_response_field: Some("paged_result_cookie".to_owned()),
            max_page_size: client.ad_max_page_size,
            injects_http_429_on_page_2: false,
            injects_http_207_partial_on_write: false,
            expected_account_id_field: "sAMAccountName".to_owned(),
            expected_status_field: "userAccountControl".to_owned(),
            expected_entitlements_field: "memberOf".to_owned(),
            raw_entitlement_verb_prefix: None,
            disconnected_entitlement_prefixes: vec![],
            expected_disconnected_itsm_queue: None,
        },
    );

    map.insert(
        "entra".to_owned(),
        MockSpokeEndpointV52 {
            connector_id: "entra".to_owned(),
            kind: ConnectorKindV52::EntraGraph,
            expected_pagination: PaginationKindV52::LinkHeader,
            expected_cursor_request_param: Some("$skiptoken".to_owned()),
            expected_cursor_response_field: Some("@odata.nextLink".to_owned()),
            max_page_size: client.entra_max_page_size,
            injects_http_429_on_page_2: true,
            injects_http_207_partial_on_write: false,
            expected_account_id_field: "userPrincipalName".to_owned(),
            expected_status_field: "accountEnabled".to_owned(),
            expected_entitlements_field: "memberOf".to_owned(),
            raw_entitlement_verb_prefix: None,
            disconnected_entitlement_prefixes: vec![],
            expected_disconnected_itsm_queue: None,
        },
    );

    if spec.has_sap {
        map.insert(
            "sap".to_owned(),
            MockSpokeEndpointV52 {
                connector_id: "sap".to_owned(),
                kind: ConnectorKindV52::SapBapiCua,
                expected_pagination: PaginationKindV52::OffsetLimit,
                expected_cursor_request_param: Some(client.sap_offset_param.clone()),
                expected_cursor_response_field: Some("next_offset".to_owned()),
                max_page_size: client.sap_max_page_size,
                injects_http_429_on_page_2: false,
                injects_http_207_partial_on_write: false,
                expected_account_id_field: "BNAME".to_owned(),
                expected_status_field: "UFLAG".to_owned(),
                expected_entitlements_field: "AGR_NAME".to_owned(),
                raw_entitlement_verb_prefix: None,
                disconnected_entitlement_prefixes: vec![],
                expected_disconnected_itsm_queue: None,
            },
        );
    }

    if spec.has_epic {
        map.insert(
            "epic".to_owned(),
            MockSpokeEndpointV52 {
                connector_id: "epic".to_owned(),
                kind: ConnectorKindV52::EpicEmpSer,
                expected_pagination: PaginationKindV52::CursorToken,
                expected_cursor_request_param: Some("epic_cursor".to_owned()),
                expected_cursor_response_field: Some("next_epic_cursor".to_owned()),
                max_page_size: 50,
                injects_http_429_on_page_2: true,
                injects_http_207_partial_on_write: false,
                expected_account_id_field: "emp_id".to_owned(),
                expected_status_field: "emp_status".to_owned(),
                expected_entitlements_field: "security_templates".to_owned(),
                raw_entitlement_verb_prefix: None,
                disconnected_entitlement_prefixes: vec![],
                expected_disconnected_itsm_queue: None,
            },
        );
    }

    if spec.has_aws {
        map.insert(
            "aws".to_owned(),
            MockSpokeEndpointV52 {
                connector_id: "aws".to_owned(),
                kind: ConnectorKindV52::AwsIamSso,
                expected_pagination: PaginationKindV52::CursorToken,
                expected_cursor_request_param: Some("NextToken".to_owned()),
                expected_cursor_response_field: Some("NextToken".to_owned()),
                max_page_size: client.aws_max_page_size,
                injects_http_429_on_page_2: true,
                injects_http_207_partial_on_write: false,
                expected_account_id_field: "user_name".to_owned(),
                expected_status_field: "state".to_owned(),
                expected_entitlements_field: "permission_sets".to_owned(),
                raw_entitlement_verb_prefix: None,
                disconnected_entitlement_prefixes: vec![],
                expected_disconnected_itsm_queue: None,
            },
        );
    }

    let mut disconnected_prefixes = client.custom_rest_disconnected_prefixes.clone();
    if spec.family_id == "F08" {
        disconnected_prefixes[0].push_str("LEGACY_");
    }

    map.insert(
        "custom_rest".to_owned(),
        MockSpokeEndpointV52 {
            connector_id: "custom_rest".to_owned(),
            kind: ConnectorKindV52::CustomRest,
            expected_pagination: PaginationKindV52::CursorToken,
            expected_cursor_request_param: Some(client.custom_rest_cursor_param.clone()),
            expected_cursor_response_field: Some(client.custom_rest_cursor_response.clone()),
            max_page_size: client.custom_rest_page_size,
            injects_http_429_on_page_2: client.custom_rest_injects_429,
            injects_http_207_partial_on_write: client.custom_rest_injects_207,
            expected_account_id_field: client.custom_rest_account_id_field.clone(),
            expected_status_field: client.custom_rest_status_field.clone(),
            expected_entitlements_field: client.custom_rest_entitlements_field.clone(),
            raw_entitlement_verb_prefix: Some(client.custom_rest_verb_prefix.clone()),
            disconnected_entitlement_prefixes: disconnected_prefixes,
            expected_disconnected_itsm_queue: Some(client.custom_rest_itsm_queue.clone()),
        },
    );

    map
}
