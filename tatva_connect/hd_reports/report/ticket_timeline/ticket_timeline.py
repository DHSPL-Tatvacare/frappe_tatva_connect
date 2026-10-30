# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Everything that happened to one ticket, newest first: edits, activity lines, comments, emails and assignments, one indexed read per kind."""
import frappe
from frappe import _
from frappe.utils import cstr, strip_html

from tatva_connect.helpdesk import TICKET
from tatva_connect.lead.report.lead_edit_history.lead_edit_history import edits

PER_KIND = 500
CLOSED = ("Closed", "Cancelled")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(TICKET, "read", filters.ticket, throw=True)
	ticket = filters.ticket
	meta = frappe.get_meta(TICKET)
	rows = []
	for v in frappe.get_all("Version", filters={"ref_doctype": TICKET, "docname": ticket},  # authz-ok: tier-b — frappe.has_permission on the ticket above is the gate
			fields=["name", "creation", "owner", "data"], order_by="creation desc", limit=PER_KIND):
		changed = list(edits(meta, frappe.parse_json(v.data) or {}, _("Ticket")))
		if changed:
			detail = "; ".join(f"{field}: {before or '-'} → {after or '-'}" for _section, field, before, after in changed)
			rows.append(_row(v.creation, _("Edit"), "", v.owner, detail[:300], "Version", v.name))
	# Helpdesk v1.31.0 keeps agent comments and activity lines in Comment; its patches copy the old rows and leave them.
	for c in frappe.get_all("Comment", filters={"reference_doctype": TICKET, "reference_name": ticket, "comment_type": ["in", ("Info", "Comment")]},  # authz-ok: tier-b — gated above
			fields=["name", "creation", "comment_email", "owner", "comment_type", "content"], order_by="creation desc", limit=PER_KIND):
		rows.append(_row(c.creation, _("Comment"), c.comment_type, c.comment_email or c.owner, _text(c.content), "Comment", c.name))
	for m in frappe.get_all("Communication", filters={"reference_doctype": TICKET, "reference_name": ticket},  # authz-ok: tier-b — gated above
			fields=["name", "creation", "communication_date", "sent_or_received", "sender", "subject", "status"], order_by="creation desc", limit=PER_KIND):
		rows.append(_row(m.communication_date or m.creation, _("Email"), f"{m.sent_or_received} · {m.status or '-'}", None,
			_("{0}: {1}").format(m.sender or "-", m.subject or ""), "Communication", m.name))
	for t in frappe.get_all("ToDo", filters={"reference_type": TICKET, "reference_name": ticket},  # authz-ok: tier-b — gated above
			fields=["name", "creation", "modified", "status", "allocated_to", "assigned_by", "owner"], order_by="creation desc", limit=PER_KIND):
		rows.append(_row(t.creation, _("Assigned"), "", t.assigned_by or t.owner, _("to {0}").format(t.allocated_to or "-"), "ToDo", t.name))
		if t.status in CLOSED:
			rows.append(_row(t.modified, _("Assignment {0}").format(t.status.lower()), t.status, None, _("was {0}").format(t.allocated_to or "-"), "ToDo", t.name))
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


def _text(html):
	return strip_html(cstr(html)).strip()[:160]


def _row(when, activity, status, who, detail, record_type, record):
	return {"when": when, "activity": activity, "status": status, "who": who, "detail": detail, "record_type": record_type, "record": cstr(record)}
