# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Index HD Ticket contact, opening_date and resolution_date: the HD Reports filter and count on them, and without an index each read scans every ticket.

This patch builds the three indexes on the first migrate. Their `search_index` Property Setters (fixtures) keep them: a schema sync drops a
single-column index whose field does not declare search_index (frappe schema.py:311), and `add_index` writes no setter during a migrate.
"""
import frappe

TICKET = "tabHD Ticket"
FIELDS = ("contact", "opening_date", "resolution_date")


def execute():
	for field in FIELDS:
		index = f"{field}_index"
		if frappe.db.has_index(TICKET, index):
			continue
		try:
			frappe.db.add_index("HD Ticket", [field], index)
		except Exception:
			frappe.log_error(title=f"HD Ticket {field} index failed", message=frappe.get_traceback())
