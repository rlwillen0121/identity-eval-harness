"""Hand-written roles (SPEC §6.2).

Birthright roles are assigned from HR attributes; business roles are requested. Department
birthright roles nest business roles (``role_fin_ops`` -> ``role_gl_close`` ->
``role_ledger_reader``), so held access only shows up after expanding nested roles.

Separation of duties: each department's birthright confers at most one side of any SoD rule
(Finance and Accounting post journals, Procurement raises POs, Operations receives goods). The
other sides only arrive through requested business roles or entitlements, which is how the
benchmark gets real, request-created conflicts. ``catalog`` verifies this for every attribute
combination at build time.
"""

from __future__ import annotations

from identitybench.world.entities import RoleKind
from identitybench.world.specs import (
    EMP,
    EVERYONE,
    STAFF,
    STARTUP_TIER,
    VENDOR_ONLY,
    RoleSpec,
)

BR, BIZ = RoleKind.birthright, RoleKind.business

ROLE_SPECS: tuple[RoleSpec, ...] = (
    # ------------------------------------------------------------------ company-wide birthright
    RoleSpec(
        "role_all_staff", "All Staff", BR,
        entitlements=(
            "ent_idp_sso_user", "ent_mail_mailbox", "ent_chat_member", "ent_wiki_reader",
            "ent_hris_self_service", "ent_vpn_user", "ent_pwmgr_user", "ent_lms_learner",
            "ent_itsm_requester", "ent_iga_request_submit",
        ),
        employment_types=STAFF, min_tier=STARTUP_TIER,
    ),
    RoleSpec(
        "role_employee_base", "Employee Benefits and Expenses", BR,
        entitlements=("ent_benefits_enroll", "ent_expense_submitter",
                      "ent_proc_requisition_create"),
        employment_types=EMP,
    ),
    RoleSpec(
        "role_vendor_base", "Vendor Access", BR,
        entitlements=(
            "ent_idp_sso_user", "ent_chat_guest", "ent_mail_vendor_mailbox", "ent_vpn_vendor",
            "ent_itsm_requester", "ent_lms_learner",
        ),
        employment_types=VENDOR_ONLY, min_tier=STARTUP_TIER,
    ),
    RoleSpec(
        "role_eu_staff", "EU Workforce", BR, entitlements=("ent_euhr_self_service",),
        employment_types=STAFF, regions=("EU",),
    ),
    RoleSpec(
        "role_apac_staff", "APAC Workforce", BR, entitlements=("ent_apacpay_self_service",),
        employment_types=STAFF, regions=("APAC",),
    ),
    # ------------------------------------------------------- department birthright (employees)
    RoleSpec(
        "role_exec_core", "Executive Office", BR,
        entitlements=("ent_fpa_models_view", "ent_bi_viewer", "ent_board_reader"),
        children=("role_finance_reporting",),
        departments=("Executive",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_fin_ops", "Finance Operations", BR,
        entitlements=("ent_ap_invoice_view", "ent_fpa_models_edit", "ent_treasury_cash_view",
                      "ent_expense_auditor"),
        children=("role_gl_close", "role_finance_reporting"),
        departments=("Finance",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_acct_ops", "Accounting Operations", BR,
        entitlements=("ent_ap_invoice_entry", "ent_ap_invoice_view", "ent_revrec_view",
                      "ent_fa_view", "ent_billing_invoice_view", "ent_tax_view"),
        children=("role_gl_close",),
        departments=("Accounting",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_proc_ops", "Procurement Operations", BR,
        entitlements=("ent_ap_invoice_view", "ent_clm_view"),
        children=("role_buyer",),
        departments=("Procurement",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_sales_core", "Sales", BR,
        entitlements=("ent_crm_sales_user", "ent_cpq_quote_create", "ent_clm_view",
                      "ent_bi_viewer"),
        departments=("Sales",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_mkt_core", "Marketing", BR,
        entitlements=("ent_mkt_campaign_editor", "ent_crm_read", "ent_cms_editor",
                      "ent_design_viewer", "ent_pa_viewer", "ent_bi_viewer"),
        departments=("Marketing",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_sup_core", "Customer Support", BR,
        entitlements=("ent_support_agent", "ent_crm_read", "ent_billing_invoice_view",
                      "ent_obs_view"),
        departments=("Customer Support",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_eng_core", "Engineering", BR,
        entitlements=(
            "ent_code_write", "ent_relpipe_build_run", "ent_relpipe_deploy_nonprod",
            "ent_cloudsbx_developer", "ent_k8s_staging_deploy", "ent_obs_view",
            "ent_proj_member", "ent_paging_responder", "ent_vault_nonprod_read",
        ),
        children=("role_eng_prod_read",),
        departments=("Engineering",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_prod_core", "Product", BR,
        entitlements=("ent_proj_member", "ent_design_editor", "ent_pa_viewer", "ent_code_read",
                      "ent_flags_view", "ent_bi_viewer"),
        departments=("Product",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_data_core", "Data and Analytics", BR,
        entitlements=("ent_dwh_analyst_read", "ent_bi_author", "ent_etl_view", "ent_nb_user",
                      "ent_code_write"),
        children=("role_bi_viewer",),
        departments=("Data",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_it_ops", "IT Operations", BR,
        entitlements=("ent_itsm_agent", "ent_mdm_view", "ent_idp_helpdesk",
                      "ent_idp_reports_view", "ent_obs_view", "ent_vuln_view"),
        departments=("IT",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_sec_ops", "Security Operations", BR,
        entitlements=("ent_siem_view", "ent_vuln_view", "ent_idp_reports_view", "ent_mdm_view",
                      "ent_iga_reports_view", "ent_cloudprod_readonly"),
        departments=("Security",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_hr_core", "People Operations", BR,
        entitlements=("ent_hris_employee_view", "ent_ats_recruiter", "ent_lms_author",
                      "ent_benefits_admin_view"),
        departments=("HR",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_legal_core", "Legal", BR,
        entitlements=("ent_clm_editor", "ent_ediscovery_view", "ent_board_reader"),
        departments=("Legal",), employment_types=EMP, derive_extended=True,
    ),
    RoleSpec(
        "role_ops_core", "Operations", BR,
        entitlements=("ent_wms_inventory_view", "ent_wms_pick_pack", "ent_tms_view",
                      "ent_badge_view"),
        children=("role_receiving",),
        departments=("Operations",), employment_types=EMP, derive_extended=True,
    ),
    # ---------------------------------------------------------- startup department birthright
    RoleSpec(
        "role_eng_core", "Engineering and Product", BR,
        entitlements=("ent_code_write", "ent_relpipe_build_run", "ent_relpipe_deploy_nonprod",
                      "ent_itsm_change_submit"),
        departments=("Engineering", "Product"), employment_types=EMP,
        min_tier=STARTUP_TIER, max_tier=STARTUP_TIER,
    ),
    RoleSpec(
        "role_gtm_core", "Go-to-Market", BR,
        entitlements=("ent_crm_sales_user",),
        departments=("Sales", "Marketing", "Customer Support"), employment_types=EMP,
        min_tier=STARTUP_TIER, max_tier=STARTUP_TIER,
    ),
    RoleSpec(
        "role_fin_ops", "Finance Operations", BR,
        entitlements=("ent_ap_invoice_view", "ent_ap_invoice_entry", "ent_gl_ledger_view"),
        children=("role_gl_close",),
        departments=("Finance",), employment_types=EMP,
        min_tier=STARTUP_TIER, max_tier=STARTUP_TIER,
    ),
    RoleSpec(
        "role_people_ops", "People Operations", BR,
        entitlements=("ent_payroll_register_view",),
        departments=("HR",), employment_types=EMP,
        min_tier=STARTUP_TIER, max_tier=STARTUP_TIER,
    ),
    # ------------------------------------------------------------------------- business roles
    RoleSpec(
        "role_ledger_reader", "Ledger Read Access", BIZ,
        entitlements=("ent_gl_ledger_view", "ent_bi_finance_view"),
        audience=("Finance", "Accounting", "Executive", "Data"),
    ),
    RoleSpec(
        "role_gl_close", "GL Close Team", BIZ,
        entitlements=("ent_gl_je_post", "ent_gl_je_post_bulk"),
        children=("role_ledger_reader",),
        audience=("Finance", "Accounting"), min_tier=STARTUP_TIER,
    ),
    RoleSpec(
        "role_gl_reviewer", "Journal Entry Review", BIZ,
        entitlements=("ent_gl_je_approve", "ent_gl_period_close"),
        children=("role_ledger_reader",),
        audience=("Accounting", "Finance"), leaders_only=True, min_tier=STARTUP_TIER,
    ),
    RoleSpec(
        "role_finance_reporting", "Finance Reporting", BIZ,
        entitlements=("ent_bi_finance_view", "ent_fpa_models_view"),
        children=("role_ledger_reader",),
        audience=("Finance", "Executive", "Accounting"),
    ),
    RoleSpec(
        "role_vendor_master", "Vendor Master Maintenance", BIZ,
        entitlements=("ent_ap_vendor_create", "ent_ap_vendor_edit"),
        audience=("Accounting", "Procurement"),
    ),
    RoleSpec(
        "role_ap_processing", "AP Processing", BIZ,
        entitlements=("ent_ap_invoice_entry", "ent_ap_invoice_view"),
        audience=("Accounting", "Finance"),
    ),
    RoleSpec(
        "role_payment_approver", "Payment Approval", BIZ,
        entitlements=("ent_ap_payment_approve", "ent_treasury_payment_release"),
        audience=("Finance", "Accounting"), leaders_only=True, min_tier=STARTUP_TIER,
    ),
    RoleSpec(
        "role_payroll_processor", "Payroll Processing", BIZ,
        entitlements=("ent_payroll_edit", "ent_payroll_bulk_import", "ent_payroll_register_view"),
        audience=("HR", "Accounting"),
    ),
    RoleSpec(
        "role_payroll_approver", "Payroll Approval", BIZ,
        entitlements=("ent_payroll_run_approve", "ent_payroll_register_view"),
        audience=("HR", "Accounting", "Finance"), leaders_only=True,
    ),
    RoleSpec(
        "role_apac_payroll_ops", "APAC Payroll Operations", BIZ,
        entitlements=("ent_apacpay_processor",),
        audience=("HR", "Accounting"),
    ),
    RoleSpec(
        "role_buyer", "Buyer", BIZ,
        entitlements=("ent_proc_po_create", "ent_proc_po_change", "ent_proc_requisition_create"),
        audience=("Procurement", "Operations"),
    ),
    RoleSpec(
        "role_receiving", "Goods Receiving", BIZ,
        entitlements=("ent_proc_goods_receipt", "ent_wms_receive"),
        audience=("Operations", "Procurement"),
    ),
    RoleSpec(
        "role_cab_member", "Change Advisory Board", BIZ,
        entitlements=("ent_itsm_change_approve", "ent_itsm_change_submit"),
        audience=("IT", "Engineering", "Security"), leaders_only=True,
    ),
    RoleSpec(
        "role_eng_prod_read", "Production Read Access", BIZ,
        entitlements=("ent_cloudprod_readonly", "ent_k8s_prod_view"),
        audience=("Engineering", "IT", "Data"),
    ),
    RoleSpec(
        "role_release_manager", "Release Management (SOX)", BIZ,
        entitlements=("ent_relpipe_deploy_sox_prod", "ent_code_deploy_prod"),
        children=("role_eng_prod_read",),
        audience=("Engineering",), min_tier=STARTUP_TIER,
    ),
    RoleSpec(
        "role_sre_oncall", "SRE On-Call", BIZ,
        entitlements=("ent_k8s_prod_deploy", "ent_cloudprod_operator",
                      "ent_paging_schedule_edit", "ent_obs_alerts_admin"),
        children=("role_eng_prod_read",),
        audience=("Engineering", "IT"),
    ),
    RoleSpec(
        "role_idp_admin", "Identity Platform Administration", BIZ,
        entitlements=("ent_idp_user_admin", "ent_idp_super_admin", "ent_idp_app_admin"),
        audience=("IT", "Security"), min_tier=STARTUP_TIER,
    ),
    RoleSpec(
        "role_access_approver", "Access Request Approval", BIZ,
        entitlements=("ent_iga_request_approve", "ent_iga_campaign_reviewer"),
        audience=("IT", "Security", "Engineering", "Finance", "Accounting", "Operations"),
        leaders_only=True,
    ),
    RoleSpec(
        "role_bi_viewer", "BI Viewer", BIZ, entitlements=("ent_bi_viewer",),
        audience=(EVERYONE,),
    ),
    RoleSpec(
        "role_data_analyst", "Data Analyst", BIZ,
        entitlements=("ent_dwh_analyst_read", "ent_bi_author"),
        children=("role_bi_viewer",),
        audience=("Data", "Finance", "Marketing", "Product"),
    ),
    RoleSpec(
        "role_data_engineer", "Data Engineering", BIZ,
        entitlements=("ent_dwh_engineer", "ent_etl_developer", "ent_etl_prod_trigger"),
        children=("role_data_analyst",),
        audience=("Data", "Engineering"),
    ),
    RoleSpec(
        "role_sales_ops", "Sales Operations", BIZ,
        entitlements=("ent_crm_sales_user", "ent_cpq_quote_create", "ent_bi_author"),
        children=("role_bi_viewer",),
        audience=("Sales",),
    ),
    RoleSpec(
        "role_mkt_ops", "Marketing Operations", BIZ,
        entitlements=("ent_mkt_send_approve", "ent_mkt_campaign_editor",
                      "ent_cdp_audience_build"),
        audience=("Marketing",),
    ),
    RoleSpec(
        "role_support_lead", "Support Team Lead", BIZ,
        entitlements=("ent_support_macros_admin", "ent_billing_credit_issue", "ent_crm_read"),
        audience=("Customer Support",), leaders_only=True,
    ),
    RoleSpec(
        "role_hr_admin", "HR Administration", BIZ,
        entitlements=("ent_hris_employee_edit", "ent_hris_comp_view", "ent_ats_offer_approve"),
        audience=("HR",), leaders_only=True,
    ),
    RoleSpec(
        "role_legal_hold", "Legal Hold Administration", BIZ,
        entitlements=("ent_ediscovery_hold_manage", "ent_ediscovery_view"),
        audience=("Legal", "Security"),
    ),
    RoleSpec(
        "role_security_analyst", "Security Analyst", BIZ,
        entitlements=("ent_siem_analyst", "ent_vuln_scan_run", "ent_siem_view"),
        audience=("Security",),
    ),
    RoleSpec(
        "role_it_helpdesk", "IT Helpdesk", BIZ,
        entitlements=("ent_itsm_agent", "ent_idp_helpdesk", "ent_mdm_device_actions"),
        audience=("IT",),
    ),
    RoleSpec(
        "role_eu_data_steward", "EU Data Steward", BIZ,
        entitlements=("ent_eudv_read", "ent_eudv_erasure"),
        audience=("Customer Support", "Data"),
    ),
    RoleSpec(
        "role_wms_supervisor", "Warehouse Supervisor", BIZ,
        entitlements=("ent_wms_inventory_adjust", "ent_wms_pick_pack"),
        children=("role_receiving",),
        audience=("Operations",), leaders_only=True,
    ),
)  # fmt: skip
