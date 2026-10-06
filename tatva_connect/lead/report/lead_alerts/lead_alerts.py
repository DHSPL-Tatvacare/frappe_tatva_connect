# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Every alert raised on one lead and on its tasks: who it went to, from whom, and whether they read it."""
import frappe
from frappe import _
from frappe.utils import cstr, strip_html

LEAD = "CRM Lead"
ALERTS = 500


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(LEAD, "read", filters.lead, throw=True)
	# A task id is an int and `document_name` is text: bind it as text or the index is skipped.
	tasks = [cstr(n) for n in frappe.get_all("CRM Task", filters={"reference_doctype": LEAD, "reference_docname": filters.lead}, pluck="name")]  # authz-ok: tier-b — frappe.has_permission on the lead above is the gate
	logs = frappe.get_all("Notification Log", filters={"document_name": ["in", [filters.lead, *tasks]], "document_type": ["in", [LEAD, "CRM Task"]]},  # authz-ok: tier-b — gated above
		fields=["creation", "for_user", "from_user", "type", "subject", "read", "document_type", "document_name"], order_by="creation desc", limit=ALERTS)
	rows = [{
		"when": n.creation, "to": n.for_user, "from": n.from_user, "kind": n.type, "message": strip_html(cstr(n.subject))[:200],
		"read": n.read, "record_type": n.document_type, "record": n.document_name,
	} for n in logs]
	columns = [
		{"label": _("When"), "fieldname": "when", "fieldtype": "Datetime", "width": 160},
		{"label": _("To"), "fieldname": "to", "fieldtype": "Link", "options": "User", "width": 200},
		{"label": _("From"), "fieldname": "from", "fieldtype": "Link", "options": "User", "width": 200},
		{"label": _("Kind"), "fieldname": "kind", "fieldtype": "Data", "width": 100},
		{"label": _("Message"), "fieldname": "message", "fieldtype": "Data", "width": 360},
		{"label": _("Read"), "fieldname": "read", "fieldtype": "Check", "width": 60},
		{"label": _("On"), "fieldname": "record_type", "fieldtype": "Data", "width": 90},
		{"label": _("Record"), "fieldname": "record", "fieldtype": "Dynamic Link", "options": "record_type", "width": 110},
	]
	return columns, rows
