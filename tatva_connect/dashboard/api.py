# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The dashboard, for whoever is asking. One endpoint, one round trip, one shape.

The caller sends a date range and the filters their layout offered them, and nothing else. It cannot name
a chart, a list or a column — everything about what is shown is resolved server-side from the caller's own
roles, so the endpoint is not a way to ask for a card somebody else's role was given.

NOT CONFIGURED IS A 200. A role with no layout is an expected state on this site, not an error: the answer
is `{"configured": false}` and the dashboard says so. Raising would put a red banner in front of a person
whose only problem is that an operator has not chosen their cards yet.

ONE BAD CARD DEGRADES ONE CARD. A declaration that throws is caught, logged and returned with an `error`
key in its place, because nine working cards and one broken one is a better answer than a dead page.

Charts are loaded in ONE query for the whole layout (A3 — never one read per card), and each is executed
through `executor.run`, which is the only thing here that touches data.

Plan: docs/plans/2026-07-31-dashboard-role-layouts-phase-1.md
"""

import frappe
from frappe import _
from frappe.utils import get_first_day, get_last_day, nowdate

from tatva_connect.dashboard import executor, resolver

CHART_DOCTYPE = "CRM Dashboard Chart"

_CHART_FIELDS = (
	"chart_name",
	"label",
	"subtitle",
	"chart_type",
	"source_doctype",
	"aggregate",
	"aggregate_field",
	"group_by_field",
	"label_field",
	"date_field",
	"honours_date_range",
	"base_filters",
	"row_limit",
	"drill_enabled",
)

# Where a card sits. Carried back beside the card so the browser lays the dashboard out without a second call.
_PLACEMENT = ("x", "y", "w", "h")


@frappe.whitelist()
def get_dashboard(from_date=None, to_date=None, filters=None):
	"""Every card this person's layout places, executed. `configured: false` when no role grants one."""
	layout = resolver.layout_for()
	if not layout:
		return {"configured": False, "charts": [], "filters": []}
	placements = _placements(layout)
	declared = _declared(placement["chart"] for placement in placements)
	window = _window(from_date, to_date)
	chosen = frappe.parse_json(filters) if filters else {}
	return {
		"configured": True,
		"title": layout.get("title") or "",
		"filters": frappe.parse_json(layout.get("exposed_filters") or "[]"),
		"charts": [
			_card(placement, declared.get(placement["chart"]), window, chosen)
			for placement in placements
			if placement["chart"] in declared
		],
	}


@frappe.whitelist()
def get_chart(chart_name, from_date=None, to_date=None, filters=None):
	"""One card, refreshed on its own. Only a card the caller's OWN layout places can be asked for."""
	layout = resolver.layout_for()
	placed = {placement["chart"] for placement in _placements(layout)} if layout else set()
	if chart_name not in placed:
		# The same refusal for "no such card" and "not your card", so guessing a name learns nothing.
		frappe.throw(_("That card is not on your dashboard."), frappe.PermissionError)
	declared = _declared([chart_name])
	if chart_name not in declared:
		frappe.throw(_("That card is not on your dashboard."), frappe.PermissionError)
	placement = next(p for p in _placements(layout) if p["chart"] == chart_name)
	chosen = frappe.parse_json(filters) if filters else {}
	return _card(placement, declared[chart_name], _window(from_date, to_date), chosen)


def _placements(layout):
	parsed = frappe.parse_json(layout.get("layout") or "[]")
	return [p for p in parsed if isinstance(p, dict) and p.get("chart")]


def _declared(names):
	"""Every card the layout places, in ONE read. A disabled card is simply not returned."""
	names = list(names)
	if not names:
		return {}
	rows = frappe.get_list(
		CHART_DOCTYPE,
		filters={"chart_name": ["in", names], "enabled": 1},
		fields=list(_CHART_FIELDS),
		limit=len(names),
		ignore_permissions=True,  # authz-ok: tier-c — reads the operator chart config the caller's own layout already places
	)
	return {row["chart_name"]: row for row in rows}


def _window(from_date, to_date):
	"""An unset range is the current month — the same default the existing dashboard already applies."""
	if not from_date or not to_date:
		from_date = get_first_day(from_date or nowdate())
		to_date = get_last_day(to_date or nowdate())
	return {"from_date": str(from_date), "to_date": str(to_date)}


def _card(placement, chart, window, chosen):
	"""One card and its position. A declaration that throws costs its own card and no other."""
	card = {key: placement.get(key) for key in _PLACEMENT}
	try:
		card.update(executor.run(chart, window, chosen))
	except Exception as broken:
		frappe.log_error(title=f"Dashboard chart failed: {chart['chart_name']}", message=frappe.get_traceback())
		card.update(
			{
				"chart": chart["chart_name"],
				"type": chart["chart_type"],
				"label": chart["label"],
				"subtitle": chart.get("subtitle") or "",
				"value": 0,
				"points": [],
				"error": str(broken),
			}
		)
	return card
