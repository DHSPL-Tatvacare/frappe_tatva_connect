"""The seven lead sections; without one a field key has no table, no row key and no heading."""
import frappe

# The structure is ours, so it is declared here. The field rows that name a section stay operator data.
_ROWS = [
	{"section_key": "lead", "title": "Lead Details", "display_order": 10, "child_table_field": "", "target_doctype": "CRM Lead", "is_multi_row": 0, "row_key_field": ""},
	{"section_key": "acq", "title": "Acquisition", "display_order": 20, "child_table_field": "custom_acquisition_profile", "target_doctype": "CRM Acquisition Profile", "is_multi_row": 0, "row_key_field": ""},
	{"section_key": "plan", "title": "Plan", "display_order": 30, "child_table_field": "custom_plan_profile", "target_doctype": "CRM Plan Profile", "is_multi_row": 0, "row_key_field": ""},
	{"section_key": "lab", "title": "Lab", "display_order": 40, "child_table_field": "custom_lab_profile", "target_doctype": "CRM Lab Profile", "is_multi_row": 1, "row_key_field": "report_date"},
	{"section_key": "care", "title": "Care & Providers", "display_order": 60, "child_table_field": "custom_care_providers_profile", "target_doctype": "CRM Care Providers Profile", "is_multi_row": 0, "row_key_field": ""},
	{"section_key": "drug", "title": "Drug Program", "display_order": 70, "child_table_field": "custom_drug_program_profile", "target_doctype": "CRM Drug Program Profile", "is_multi_row": 1, "row_key_field": "cycle_date"},
	{"section_key": "metrics", "title": "Activity Metrics", "display_order": 80, "child_table_field": "custom_lead_activity_metrics", "target_doctype": "CRM Lead Activity Metrics", "is_multi_row": 0, "row_key_field": ""},
]


def ensure_rows():
	"""Idempotent: only the rows our own code depends on are asserted, and an operator's edits are left alone."""
	for row in _ROWS:
		if frappe.db.exists("CRM Lead Section", row["section_key"]):
			continue
		frappe.get_doc({"doctype": "CRM Lead Section", **row}).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
	frappe.db.commit()
