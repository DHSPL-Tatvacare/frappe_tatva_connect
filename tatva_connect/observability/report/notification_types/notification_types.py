# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One row per notification type over a window: how many went out, how many were read, and who receives it by email and push."""
from frappe import _
from frappe.utils import cint

from tatva_connect.observability import notifications_health


def execute(filters=None):
	days = cint((filters or {}).get("days")) or notifications_health.WINDOW_DAYS
	columns = [
		{"fieldname": "type", "label": _("Type"), "fieldtype": "Data", "width": 180},
		{"fieldname": "written", "label": _("Sent"), "fieldtype": "Int", "width": 90},
		{"fieldname": "read", "label": _("Read"), "fieldtype": "Int", "width": 90},
		{"fieldname": "read_rate", "label": _("Read %"), "fieldtype": "Percent", "width": 90},
		{"fieldname": "email_on", "label": _("Email On (users)"), "fieldtype": "Int", "width": 130},
		{"fieldname": "push_on", "label": _("Push On (users)"), "fieldtype": "Int", "width": 130},
		{"fieldname": "push_sent", "label": _("Pushed"), "fieldtype": "Int", "width": 90},
	]
	return columns, notifications_health.by_type(days)
