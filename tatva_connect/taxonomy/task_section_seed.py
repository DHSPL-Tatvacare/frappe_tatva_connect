"""The task sections; without one an activity field has no table, no row key and no heading."""
import frappe
from frappe.utils import cstr

# The structure is ours, so it is declared here. The field rows that name a section stay operator data,
# and so do the titles and the order — D13: the names are the operator's to change.
_ROWS = [
	{"section_key": "engagement", "title": "Engagement", "display_order": 10, "child_table_field": "custom_engagement", "target_doctype": "CRM Task Engagement", "is_multi_row": 0, "row_key_field": "", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	{"section_key": "order", "title": "Order", "display_order": 20, "child_table_field": "custom_order", "target_doctype": "CRM Task Order", "is_multi_row": 0, "row_key_field": "", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	# One row per task, like every other column section: CRM Task Document carries a COLUMN per kind, so one row already holds every kind. Multi-row was unsatisfiable here — `field_target` answers a column section with a column and never a row key, so nothing could ever address a second row, and `document_kind` was set on 0 of 941 live rows.
	{"section_key": "documents", "title": "Documents", "display_order": 30, "child_table_field": "custom_documents", "target_doctype": "CRM Task Document", "is_multi_row": 0, "row_key_field": "document_kind", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	# Key-value: the DEFAULT home. A field carrying no shape shared across types is a row of its own fieldname, so a new field is a declaration and never a schema change.
	{"section_key": "answers", "title": "Answers", "display_order": 40, "child_table_field": "custom_answers", "target_doctype": "CRM Task Answer", "is_multi_row": 0, "row_key_field": "fieldname", "is_key_value": 1, "value_field": "value", "label_field": "", "question_field": "fieldname"},
	# Key-value too, and DELIBERATELY ordered after `answers`: the untargeted fallback is the FIRST key-value section by display_order, so this one is reached only by a field that names it. Which lead fields an account asks for differs per account, so named columns here would be per-account DDL.
	{"section_key": "lead_snapshot", "title": "Lead Snapshot", "display_order": 50, "child_table_field": "custom_lead_snapshot", "target_doctype": "CRM Task Lead Snapshot", "is_multi_row": 0, "row_key_field": "fieldname", "is_key_value": 1, "value_field": "value", "label_field": "", "question_field": "fieldname"},
]


# Where a section's rows live and how one is addressed. Code reads these, so they are asserted on every
# migrate rather than left at whatever an older seed inserted. Title and order are the operator's.
_STRUCTURAL = ("target_doctype", "child_table_field", "is_multi_row", "row_key_field", "is_key_value",
               "value_field", "label_field", "question_field")


def ensure_rows():
	"""Idempotent: the structure our code depends on is asserted, and an operator's presentation is left alone."""
	if not frappe.db.table_exists("CRM Task Section"):
		return  # skip-until-ready: the doctype has not synced yet; the after_migrate pass seeds the rows then
	task_meta = frappe.get_meta("CRM Task")
	for row in _ROWS:
		if not frappe.db.exists("CRM Task Section", row["section_key"]):
			# skip-until-ready: the Table field lands in sync_fixtures, AFTER any patch caller; the after_migrate pass seeds the row then. Same contract as the lead section seed.
			if row["child_table_field"] and not task_meta.get_field(row["child_table_field"]):
				continue
			frappe.get_doc({"doctype": "CRM Task Section", **row}).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
			continue
		declared = {f: row[f] for f in _STRUCTURAL}
		stored = frappe.db.get_value("CRM Task Section", row["section_key"], _STRUCTURAL, as_dict=True)
		if any(cstr(stored[f]) != cstr(declared[f]) for f in _STRUCTURAL):
			frappe.db.set_value("CRM Task Section", row["section_key"], declared)  # authz-ok: tier-c — after_migrate, structural fields this app owns
	frappe.db.commit()
