"""Data method for the 'Reps by Grain' Dashboard Chart Source: per pool grain, the reps in it and how many are checked in.

A rep's grain is the grain of the lead pools they sit in, the same rows `entitlement` reads; a rep in two pools counts in both.
The axes come from `grain.columns("Assignment Rule")`, read off the schema and never typed here.
"""
import frappe
from frappe import _

from tatva_connect.lead import checkin
from tatva_connect.lead.desk_cards import reps
from tatva_connect.taxonomy import grain

RULE, MEMBER = "Assignment Rule", "Assignment Rule User"


@frappe.whitelist()
def get(chart_name=None, chart=None, no_cache=None, filters=None, from_date=None, to_date=None,
	time_interval=None, timespan=None, heatmap_year=None, **kwargs):
	frappe.has_permission(RULE, "read", throw=True)
	axes = [column for column in grain.columns(RULE) if column]
	pools = frappe.get_all(RULE, filters={"document_type": "CRM Lead", "disabled": 0}, fields=["name", *axes])
	label = {p.name: "::".join(p.get(axis) or "—" for axis in axes) for p in pools}
	members = frappe.get_all(MEMBER, filters={"parenttype": RULE, "parent": ["in", list(label) or [""]]}, fields=["parent", "user"])
	active = reps()
	by_grain = {}
	for row in members:
		if row.user in active:
			by_grain.setdefault(label[row.parent], set()).add(row.user)
	working = checkin.active_users(set().union(*by_grain.values()) if by_grain else set())
	order = sorted(by_grain, key=lambda g: -len(by_grain[g]))
	return {
		"labels": order,
		"datasets": [
			{"name": _("Reps"), "values": [len(by_grain[g]) for g in order]},
			{"name": _("Checked in"), "values": [len(by_grain[g] & working) for g in order]},
		],
	}
