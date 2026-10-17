# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Every ticket one contact raised, open and closed, newest first: matched on the ticket's `contact`, the rule helpdesk itself uses for a contact's tickets."""
import frappe
from frappe import _

from tatva_connect.helpdesk import TICKET

FIELDS = ("subject", "status", "ticket_type", "priority", "agent_group", "opening_date", "resolution_date",
          "first_response_time", "feedback_rating")
WIDE = {"subject": 280}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission("Contact", "read", filters.contact, throw=True)
	rows = frappe.get_list(TICKET, filters={"contact": filters.contact}, fields=["name", *FIELDS], order_by="creation desc")
	meta = frappe.get_meta(TICKET)
	columns = [{"label": _("Ticket"), "fieldname": "name", "fieldtype": "Link", "options": TICKET, "width": 90}]
	columns += [_column(meta.get_field(f)) for f in FIELDS]
	return columns, rows


def _column(df):
	return {"label": _(df.label), "fieldname": df.fieldname, "fieldtype": df.fieldtype, "options": df.options,
	        "width": WIDE.get(df.fieldname, 140)}
