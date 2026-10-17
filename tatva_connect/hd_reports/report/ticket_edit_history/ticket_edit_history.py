# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Every edit to one ticket, one row per changed value, through the same edit-history reader Lead Edit History uses."""
import frappe
from frappe import _

from tatva_connect.helpdesk import TICKET
from tatva_connect.lead.report.lead_edit_history.lead_edit_history import edit_rows


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return edit_rows(TICKET, filters.ticket, filters.field, _("Ticket"))
