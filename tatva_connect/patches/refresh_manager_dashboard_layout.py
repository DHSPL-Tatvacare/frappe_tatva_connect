# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Refresh the CRM "Manager Dashboard" to the reshaped ops layout — but ONLY if it is still the
untouched stock board.

The dashboard is a single shared record a Sales/System Manager can edit from the desk. A blind refresh
would silently wipe their customisation, so we replace the layout only when its chart set still matches
the ORIGINAL frappe/crm default (i.e. never edited). A customised board is left alone; the operator
opts in via desk "Reset to Default". Fresh sites never reach here — install.py seeds the new default.
"""
import json

import frappe

# The exact chart set of the ORIGINAL frappe/crm default. A board whose names match this set has never
# been edited, so replacing it is safe. Any drift => operator-owned => untouched.
_STOCK_NAMES = {
	"total_leads", "ongoing_deals", "won_deals", "average_won_deal_value", "average_deal_value",
	"average_time_to_close_a_lead", "average_time_to_close_a_deal", "spacer", "sales_trend",
	"forecasted_revenue", "funnel_conversion", "deals_by_stage_donut", "lost_deal_reasons",
	"leads_by_source", "deals_by_source", "deals_by_territory", "deals_by_salesperson",
}


def execute():
	name = "Manager Dashboard"
	if not frappe.db.exists("CRM Dashboard", name):
		return

	from crm.fcrm.doctype.crm_dashboard.crm_dashboard import default_manager_dashboard_layout

	current = json.loads(frappe.db.get_value("CRM Dashboard", name, "layout") or "[]")
	if {c.get("name") for c in current} != _STOCK_NAMES:
		return  # customised — do not clobber

	frappe.db.set_value("CRM Dashboard", name, "layout", default_manager_dashboard_layout())
