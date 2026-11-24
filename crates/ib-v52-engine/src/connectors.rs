//! Deterministic connector sandbox validator (`AD`, `Entra`, `SAP ECC/CUA`, `Epic EMP/SER`,
//! `AWS`, and `Custom REST`).

use std::collections::BTreeSet;

use ib_v52_schemas::{
    ConnectorKindV52, ConnectorSandboxResultV52, EnterpriseBrownfieldWorldV52,
    IgaDeploymentConfigV52, JsonPointerDiagnosticV52, json_pointer_child,
};

fn connector_config_pointer(connector_id: &str, field: &str) -> String {
    let connector = json_pointer_child("/connectors", connector_id);
    json_pointer_child(&connector, field)
}

fn push_error(
    errors: &mut Vec<String>,
    diagnostics: &mut Vec<JsonPointerDiagnosticV52>,
    message: &str,
    pointers: impl IntoIterator<Item = String>,
) {
    errors.push(message.to_owned());
    for pointer in pointers {
        diagnostics.push(JsonPointerDiagnosticV52 {
            document: "config/connectors.json".to_owned(),
            pointer,
            message: message.to_owned(),
        });
    }
}

/// Test a single connector specification in `config.connectors` against `world.mock_endpoints`.
pub fn test_connector_spec(
    world: &EnterpriseBrownfieldWorldV52,
    config: &IgaDeploymentConfigV52,
    connector_id: &str,
) -> Result<ConnectorSandboxResultV52, String> {
    let endpoint = world
        .mock_endpoints
        .get(connector_id)
        .ok_or_else(|| format!("unknown target connector endpoint '{connector_id}'"))?;

    let Some(spec) = config.connectors.connectors.get(connector_id) else {
        let message = format!("connector '{connector_id}' is missing from config/connectors.json");
        return Ok(ConnectorSandboxResultV52 {
            connector_id: connector_id.to_owned(),
            connection_ok: false,
            pagination_complete: false,
            pages_traversed: 0,
            accounts_read: 0,
            http_429_retried_cleanly: false,
            http_207_handled_cleanly: false,
            schema_extraction_ok: false,
            sample_extracted_entitlements: Vec::new(),
            errors: vec![message.clone()],
            error_diagnostics: vec![JsonPointerDiagnosticV52 {
                document: "config/connectors.json".to_owned(),
                pointer: "/connectors".to_owned(),
                message,
            }],
        });
    };

    let mut errors = Vec::new();
    let mut error_diagnostics = Vec::new();
    let total_accounts = world.spoke_accounts.get(connector_id).map_or(0, Vec::len);

    // 1. Connector kind / connection check.
    let connection_ok = spec.kind == endpoint.kind;
    if !connection_ok {
        push_error(
            &mut errors,
            &mut error_diagnostics,
            "DRIVER_PROTOCOL_ERROR: transport handshake failed against target endpoint",
            [connector_config_pointer(connector_id, "kind")],
        );
    }

    // 2. Page size check.
    let page_size_ok = spec.page_size > 0 && spec.page_size <= endpoint.max_page_size;
    if !page_size_ok {
        push_error(
            &mut errors,
            &mut error_diagnostics,
            "HTTP 400 Bad Request: requested page_size rejected by target endpoint limit",
            [connector_config_pointer(connector_id, "page_size")],
        );
    }

    // 3. Pagination & cursor checks.
    let pagination_kind_ok = spec.pagination == endpoint.expected_pagination;
    let mut cursor_ok = true;
    if endpoint.expected_cursor_request_param.is_some()
        && spec.cursor_request_param != endpoint.expected_cursor_request_param
    {
        cursor_ok = false;
    }
    if endpoint.expected_cursor_response_field.is_some()
        && spec.cursor_response_field != endpoint.expected_cursor_response_field
    {
        cursor_ok = false;
    }
    if !pagination_kind_ok || !cursor_ok {
        let mut pointers = Vec::new();
        if !pagination_kind_ok {
            pointers.push(connector_config_pointer(connector_id, "pagination"));
        }
        if endpoint.expected_cursor_request_param.is_some()
            && spec.cursor_request_param != endpoint.expected_cursor_request_param
        {
            pointers.push(connector_config_pointer(
                connector_id,
                "cursor_request_param",
            ));
        }
        if endpoint.expected_cursor_response_field.is_some()
            && spec.cursor_response_field != endpoint.expected_cursor_response_field
        {
            pointers.push(connector_config_pointer(
                connector_id,
                "cursor_response_field",
            ));
        }
        push_error(
            &mut errors,
            &mut error_diagnostics,
            "AGGREGATION_INCOMPLETE: stopped after page 1; next-page cursor/offset parameter was not accepted or response token path resolved empty",
            pointers,
        );
    }

    // Compute realistic page traversal counts before evaluating page-2 throttling.
    let effective_page_size = spec.page_size.clamp(1, endpoint.max_page_size.max(1));
    let needed_pages = if total_accounts == 0 {
        1
    } else {
        total_accounts.div_ceil(effective_page_size)
    };

    // 4. HTTP 429 & 207 handling.
    // Page-2 throttling is encountered only when traversal actually needs a second page.
    let page_2_throttled = needed_pages > 1 && endpoint.injects_http_429_on_page_2;
    let http_429_retried_cleanly = !page_2_throttled || spec.retry_on_http_429;
    if !http_429_retried_cleanly {
        push_error(
            &mut errors,
            &mut error_diagnostics,
            "HTTP 429 Too Many Requests on page 2; connector aborted without rate-limit retry",
            [connector_config_pointer(connector_id, "retry_on_http_429")],
        );
    }

    let http_207_handled_cleanly =
        !endpoint.injects_http_207_partial_on_write || spec.inspect_http_207_item_errors;
    if !http_207_handled_cleanly {
        push_error(
            &mut errors,
            &mut error_diagnostics,
            "PROVISIONING_DRIFT_WARNING: bulk write returned HTTP 207 Multi-Status with item-level errors that were ignored",
            [connector_config_pointer(
                connector_id,
                "inspect_http_207_item_errors",
            )],
        );
    }

    let (pagination_complete, pages_traversed, accounts_read) = if !connection_ok || !page_size_ok {
        (false, 0, 0)
    } else if !pagination_kind_ok || !cursor_ok || !http_429_retried_cleanly {
        (false, 1, total_accounts.min(effective_page_size))
    } else {
        (true, needed_pages, total_accounts)
    };

    // 5. Schema extraction, verb-prefix stripping, disconnected ITSM, SAP, and Epic checks.
    let mut schema_extraction_ok = true;

    let schema_fields_ok = spec.account_id_field == endpoint.expected_account_id_field
        && spec.status_field == endpoint.expected_status_field
        && spec.entitlements_field == endpoint.expected_entitlements_field;
    if !schema_fields_ok {
        schema_extraction_ok = false;
        let mut pointers = Vec::new();
        if spec.account_id_field != endpoint.expected_account_id_field {
            pointers.push(connector_config_pointer(connector_id, "account_id_field"));
        }
        if spec.status_field != endpoint.expected_status_field {
            pointers.push(connector_config_pointer(connector_id, "status_field"));
        }
        if spec.entitlements_field != endpoint.expected_entitlements_field {
            pointers.push(connector_config_pointer(connector_id, "entitlements_field"));
        }
        push_error(
            &mut errors,
            &mut error_diagnostics,
            "SCHEMA_EXTRACTION_EMPTY: configured account, status, or entitlements field resolved empty on raw target records",
            pointers,
        );
    }
    if spec.strip_entitlement_verb_prefix != endpoint.raw_entitlement_verb_prefix {
        schema_extraction_ok = false;
        push_error(
            &mut errors,
            &mut error_diagnostics,
            "ENTITLEMENT_CATALOG_MISMATCH: extracted entitlements did not match spoke group catalog IDs (check raw payload prefix handling)",
            [connector_config_pointer(
                connector_id,
                "strip_entitlement_verb_prefix",
            )],
        );
    }

    let disconnected_prefix_mismatch = !disconnected_prefixes_match(endpoint, spec);
    let disconnected_queue_mismatch = (!endpoint.disconnected_entitlement_prefixes.is_empty()
        || endpoint.expected_disconnected_itsm_queue.is_some())
        && spec.disconnected_itsm_queue != endpoint.expected_disconnected_itsm_queue;
    if disconnected_prefix_mismatch || disconnected_queue_mismatch {
        schema_extraction_ok = false;
        let mut pointers = Vec::new();
        if disconnected_prefix_mismatch {
            pointers.push(connector_config_pointer(
                connector_id,
                "disconnected_entitlement_prefixes",
            ));
        }
        if disconnected_queue_mismatch {
            pointers.push(connector_config_pointer(
                connector_id,
                "disconnected_itsm_queue",
            ));
        }
        push_error(
            &mut errors,
            &mut error_diagnostics,
            "PROVISIONING_WRITE_REJECTED: direct and manual entitlement namespaces or manual ITSM queue do not match the target contract",
            pointers,
        );
    }

    if endpoint.kind == ConnectorKindV52::SapBapiCua
        && (spec.sap_lock_uflag_code != Some(64) || !spec.sap_set_gltgb_to_cutover_on_disable)
    {
        schema_extraction_ok = false;
        let mut pointers = Vec::new();
        if spec.sap_lock_uflag_code != Some(64) {
            pointers.push(connector_config_pointer(
                connector_id,
                "sap_lock_uflag_code",
            ));
        }
        if !spec.sap_set_gltgb_to_cutover_on_disable {
            pointers.push(connector_config_pointer(
                connector_id,
                "sap_set_gltgb_to_cutover_on_disable",
            ));
        }
        push_error(
            &mut errors,
            &mut error_diagnostics,
            "SAP_LOCK_AUDIT_FAILURE: simulated disable left USR02 unlocked or validity window open past cutover",
            pointers,
        );
    }

    if endpoint.kind == ConnectorKindV52::EpicEmpSer
        && (!spec.epic_link_emp_to_ser || !spec.epic_require_active_license_for_ser)
    {
        schema_extraction_ok = false;
        let mut pointers = Vec::new();
        if !spec.epic_link_emp_to_ser {
            pointers.push(connector_config_pointer(
                connector_id,
                "epic_link_emp_to_ser",
            ));
        }
        if !spec.epic_require_active_license_for_ser {
            pointers.push(connector_config_pointer(
                connector_id,
                "epic_require_active_license_for_ser",
            ));
        }
        push_error(
            &mut errors,
            &mut error_diagnostics,
            "EPIC_CLINICAL_AUDIT_FAILURE: simulated credential suspension left clinical SER active or unlinked from EMP",
            pointers,
        );
    }

    // Build realistic sample extracted entitlements (simulating raw verb prefix + configured strip).
    let mut raw_entitlements = BTreeSet::new();
    if let Some(accts) = world.spoke_accounts.get(connector_id) {
        for acct in accts {
            for ent in &acct.entitlement_ids {
                raw_entitlements.insert(ent.clone());
            }
        }
    }
    if raw_entitlements.is_empty() {
        for grp in world.group_catalog.values() {
            if grp.spoke_id == connector_id {
                raw_entitlements.insert(grp.group_id.clone());
            }
        }
    }

    let sample_extracted_entitlements = raw_entitlements
        .into_iter()
        .take(8)
        .map(|ent| {
            let api_val = match &endpoint.raw_entitlement_verb_prefix {
                Some(raw_pfx) if !ent.starts_with(raw_pfx.as_str()) => format!("{raw_pfx}{ent}"),
                _ => ent,
            };
            match &spec.strip_entitlement_verb_prefix {
                Some(strip_pfx) => api_val
                    .strip_prefix(strip_pfx.as_str())
                    .unwrap_or(&api_val)
                    .to_owned(),
                None => api_val,
            }
        })
        .collect();

    Ok(ConnectorSandboxResultV52 {
        connector_id: connector_id.to_owned(),
        connection_ok,
        pagination_complete,
        pages_traversed,
        accounts_read,
        http_429_retried_cleanly,
        http_207_handled_cleanly,
        schema_extraction_ok,
        sample_extracted_entitlements,
        errors,
        error_diagnostics,
    })
}

/// Compare exact namespace scopes; a wider sibling prefix wrongly routes connected writes.
pub(crate) fn disconnected_prefixes_match(
    endpoint: &ib_v52_schemas::MockSpokeEndpointV52,
    connector: &ib_v52_schemas::ConnectorSpecV52,
) -> bool {
    let expected: BTreeSet<_> = endpoint.disconnected_entitlement_prefixes.iter().collect();
    let actual: BTreeSet<_> = connector.disconnected_entitlement_prefixes.iter().collect();
    actual == expected
}

/// Returns `true` iff a connector sandbox result passed every check cleanly.
#[must_use]
pub fn is_connector_result_passing(res: &ConnectorSandboxResultV52) -> bool {
    res.connection_ok
        && res.pagination_complete
        && res.http_429_retried_cleanly
        && res.http_207_handled_cleanly
        && res.schema_extraction_ok
        && res.errors.is_empty()
}
