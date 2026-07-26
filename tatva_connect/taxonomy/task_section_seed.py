"""The task sections; without one an activity field has no table, no row key and no heading."""
import frappe
from frappe.utils import cstr

# The structure is ours, so it is declared here. The field rows that name a section stay operator data,
# and so do the titles and the order — D13: the names are the operator's to change.
_ROWS = [
	{"section_key": "engagement", "title": "Engagement", "tab": "", "display_order": 10, "depends_on": "", "child_table_field": "custom_engagement", "target_doctype": "CRM Task Engagement", "is_multi_row": 0, "row_key_field": "", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	{"section_key": "order", "title": "Order", "tab": "", "display_order": 20, "depends_on": "", "child_table_field": "custom_order", "target_doctype": "CRM Task Order", "is_multi_row": 0, "row_key_field": "", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	# Multi-row: an upload group is one row per kind of document, so a second kind never overwrites the first.
	{"section_key": "documents", "title": "Documents", "tab": "", "display_order": 30, "depends_on": "", "child_table_field": "custom_documents", "target_doctype": "CRM Task Document", "is_multi_row": 1, "row_key_field": "document_kind", "is_key_value": 0, "value_field": "", "label_field": "", "question_field": ""},
	# Key-value: the DEFAULT home. A field carrying no shape shared across types is a row of its own fieldname, so a new field is a declaration and never a schema change.
	{"section_key": "answers", "title": "Answers", "tab": "", "display_order": 40, "depends_on": "", "child_table_field": "custom_answers", "target_doctype": "CRM Task Answer", "is_multi_row": 0, "row_key_field": "fieldname", "is_key_value": 1, "value_field": "value", "label_field": "", "question_field": "fieldname"},
]


# Where a section's rows live and how one is addressed. Code reads these, so they are asserted on every
# migrate rather than left at whatever an older seed inserted. Title, tab, order and depends_on are
# presentation and stay the operator's.
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
