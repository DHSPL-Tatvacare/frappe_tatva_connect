# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The CRM Setup desk's number cards: who can take leads right now. Each reads the rule that already decides it, and opens the records it counted."""
import frappe
from frappe.utils import getdate, today

from tatva_connect.access import ledger
from tatva_connect.lead import checkin
from tatva_connect.lead.assignment_rule import LEAVE_DT, leaves
from tatva_connect.observability.jobs_health import _card


def reps():
	"""Enabled users who hold the rep role."""
	holders = frappe.get_all("Has Role", filters={"role": ledger.SALES_USER, "parenttype": "User"}, pluck="parent")
	return set(frappe.get_all("User", filters={"name": ["in", holders or [""]], "enabled": 1}, pluck="name"))


def _opens(value, doctype, **filters):
	"""A Custom card's value plus the list it opens on click, which frappe's number card widget follows."""
	return {**_card(value), "route": ["List", doctype], "route_options": filters}


@frappe.whitelist()
def card_active_reps(filters=None) -> dict:
	"""Enabled users holding the rep role: the people a pool can hand a lead to."""
	frappe.has_permission(checkin.DOCTYPE, "read", throw=True)
	return _opens(len(reps()), "User", **{"enabled": 1, "Has Role.role": ledger.SALES_USER})


@frappe.whitelist()
def card_reps_checked_in(filters=None) -> dict:
	"""Reps whose latest check-in says Active."""
	frappe.has_permission(checkin.DOCTYPE, "read", throw=True)
	return _opens(len(checkin.active_users(reps())), checkin.DOCTYPE, status=checkin.ACTIVE)


@frappe.whitelist()
def card_reps_on_leave_today(filters=None) -> dict:
	"""Reps with a leave spell covering today."""
	frappe.has_permission(LEAVE_DT, "read", throw=True)
	day = getdate(today())
	return _opens(len(leaves(reps(), day, day)), LEAVE_DT, from_date=["<=", str(day)], to_date=[">=", str(day)])
