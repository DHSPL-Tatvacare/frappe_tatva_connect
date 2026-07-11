"""Index CRM Call Log (reference_doctype, reference_docname).

`call_list` filters on exactly this pair and the table carried no index for it, so every call a
partner made to list a lead's calls full-scanned CRM Call Log. CRM Task already carries the
equivalent (ix_refdoc_tasktype_status), so the pattern is the app's own — the call log was simply
never given one.
"""
import frappe

INDEX = "ix_calllog_reference"
FIELDS = ["reference_doctype", "reference_docname"]


def execute():
	if frappe.db.has_index("tabCRM Call Log", INDEX):
		return
	try:
		frappe.db.add_index("CRM Call Log", FIELDS, INDEX)
	except Exception:
		frappe.log_error(
			title="CRM Call Log reference index failed",
			message=frappe.get_traceback(),
		)
