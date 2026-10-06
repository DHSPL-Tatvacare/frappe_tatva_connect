# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Everything that touched one lead, newest first: calls, WhatsApp messages, tasks and notes, one indexed read per kind."""
import frappe
from frappe import _
from frappe.utils import cstr, strip_html

LEAD = "CRM Lead"
PER_KIND = 500
CLOSED = ("Done", "Canceled")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(LEAD, "read", filters.lead, throw=True)
	lead = filters.lead
	rows = []
	for c in frappe.get_all("CRM Call Log", filters={"reference_doctype": LEAD, "reference_docname": lead},  # authz-ok: tier-b — frappe.has_permission on the lead above is the gate
			fields=["name", "creation", "start_time", "type", "status", "duration", "caller", "receiver", "custom_end_reason"], order_by="creation desc", limit=PER_KIND):
		rows.append(_row(c.start_time or c.creation, _("Call"), f"{c.type} · {c.status}", c.caller if c.type == "Outgoing" else c.receiver,
			", ".join(x for x in (f"{int(c.duration or 0)}s", c.custom_end_reason) if x), "CRM Call Log", c.name))
	for w in frappe.get_all("WhatsApp Message", filters={"reference_doctype": LEAD, "reference_name": lead},  # authz-ok: tier-b — gated above
			fields=["name", "creation", "type", "status", "owner", "message", "template", "content_type", "custom_failed_reason"], order_by="creation desc", limit=PER_KIND):
		text = w.template or strip_html(cstr(w.message)).strip() or w.content_type
		rows.append(_row(w.creation, _("WhatsApp"), f"{w.type} · {w.status or '-'}", w.owner if w.type == "Outgoing" else None,
			cstr(w.custom_failed_reason or text)[:160], "WhatsApp Message", w.name))
	for t in frappe.get_all("CRM Task", filters={"reference_doctype": LEAD, "reference_docname": lead},  # authz-ok: tier-b — gated above
			fields=["name", "creation", "modified", "title", "custom_task_type", "status", "assigned_to", "owner"], order_by="creation desc", limit=PER_KIND):
		kind = cstr(t.custom_task_type).rsplit("::", 1)[-1] or t.title
		rows.append(_row(t.creation, _("Task created"), "", t.owner, _("{0}, held by {1}").format(kind, t.assigned_to or "-"), "CRM Task", t.name))
		if t.status in CLOSED:
			rows.append(_row(t.modified, _("Task {0}").format(t.status.lower()), t.status, t.assigned_to, kind, "CRM Task", t.name))
	for n in frappe.get_all("FCRM Note", filters={"reference_doctype": LEAD, "reference_docname": lead},  # authz-ok: tier-b — gated above
			fields=["name", "creation", "owner", "title"], order_by="creation desc", limit=PER_KIND):
		rows.append(_row(n.creation, _("Note"), "", n.owner, n.title, "FCRM Note", n.name))
	rows.sort(key=lambda r: r["when"], reverse=True)
	columns = [
		{"label": _("When"), "fieldname": "when", "fieldtype": "Datetime", "width": 160},
		{"label": _("Activity"), "fieldname": "activity", "fieldtype": "Data", "width": 130},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 170},
		{"label": _("Who"), "fieldname": "who", "fieldtype": "Link", "options": "User", "width": 200},
		{"label": _("Detail"), "fieldname": "detail", "fieldtype": "Data", "width": 340},
		{"label": _("Type"), "fieldname": "record_type", "fieldtype": "Data", "width": 120},
		{"label": _("Record"), "fieldname": "record", "fieldtype": "Dynamic Link", "options": "record_type", "width": 120},
	]
	return columns, rows


def _row(when, activity, status, who, detail, record_type, record):
	return {"when": when, "activity": activity, "status": status, "who": who, "detail": detail, "record_type": record_type, "record": cstr(record)}
