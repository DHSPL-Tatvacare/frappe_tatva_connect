"""The lead sections; without one a field key has no table, no row key and no heading."""
import frappe
from frappe.utils import cstr

# The structure is ours, so it is declared here. The field rows that name a section stay operator data.
_ROWS = [
	{"section_key": "lead", "title": "Lead Details", "display_order": 10, "child_table_field": "", "target_doctype": "CRM Lead", "is_multi_row": 0, "row_key_field": "", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	# Multi-row: a patient is acquired more than once, so every campaign touch is its own row.
	{"section_key": "acq", "title": "Acquisition", "display_order": 20, "child_table_field": "custom_acquisition_profile", "target_doctype": "CRM Acquisition Profile", "is_multi_row": 1, "row_key_field": "touch_at", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	{"section_key": "plan", "title": "Plan", "display_order": 30, "child_table_field": "custom_plan_profile", "target_doctype": "CRM Plan Profile", "is_multi_row": 0, "row_key_field": "", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	# Multi-row: a patient buys more than once, so every purchase is its own row. Keyed on the start date and never the SKU, which the rep must stay able to pick.
	{"section_key": "products", "title": "Plan Purchased", "display_order": 35, "child_table_field": "products", "target_doctype": "CRM Products", "is_multi_row": 1, "row_key_field": "custom_start_date", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	{"section_key": "lab", "title": "Lab", "display_order": 40, "child_table_field": "custom_lab_profile", "target_doctype": "CRM Lab Profile", "is_multi_row": 1, "row_key_field": "report_date", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	{"section_key": "screening", "title": "Screening", "display_order": 50, "child_table_field": "custom_screening_answers", "target_doctype": "CRM Lead Screening Answer", "is_multi_row": 0, "row_key_field": "question_hash", "is_key_value": 1, "value_field": "value", "label_field": "label", "question_field": "question"},
	{"section_key": "care", "title": "Care & Providers", "display_order": 60, "child_table_field": "custom_care_providers_profile", "target_doctype": "CRM Care Providers Profile", "is_multi_row": 0, "row_key_field": "", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	{"section_key": "drug", "title": "Drug Program", "display_order": 70, "child_table_field": "custom_drug_program_profile", "target_doctype": "CRM Drug Program Profile", "is_multi_row": 1, "row_key_field": "cycle_date", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	{"section_key": "metrics", "title": "Activity Metrics", "display_order": 80, "child_table_field": "custom_lead_activity_metrics", "target_doctype": "CRM Lead Activity Metrics", "is_multi_row": 0, "row_key_field": "", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
]


# Where a section's rows live and how one is addressed. Code reads these, so they are asserted on every
# migrate rather than left at whatever an older seed inserted. Title and display_order are presentation
# and stay the operator's.
_STRUCTURAL = ("target_doctype", "child_table_field", "is_multi_row", "row_key_field", "is_key_value",
               "value_field", "label_field", "question_field")


def ensure_rows():
	"""Idempotent: the structure our code depends on is asserted, and an operator's presentation is left alone."""
	lead_meta = frappe.get_meta("CRM Lead")
	for row in _ROWS:
		if not frappe.db.exists("CRM Lead Section", row["section_key"]):
			# skip-until-ready: a pre-fixtures patch caller can run before this child field syncs; the after_migrate pass seeds the row then. Same contract as the grain seeds' _masters_exist guard.
			if row["child_table_field"] and not lead_meta.get_field(row["child_table_field"]):
				continue
			frappe.get_doc({"doctype": "CRM Lead Section", **row}).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
			continue
		declared = {f: row[f] for f in _STRUCTURAL}
		stored = frappe.db.get_value("CRM Lead Section", row["section_key"], _STRUCTURAL, as_dict=True)
		if any(cstr(stored[f]) != cstr(declared[f]) for f in _STRUCTURAL):
			frappe.db.set_value("CRM Lead Section", row["section_key"], declared)  # authz-ok: tier-c — after_migrate, structural fields this app owns
	frappe.db.commit()
