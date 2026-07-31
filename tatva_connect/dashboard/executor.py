# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""A declaration in, a card out. The ONE place in this module that touches data.

EVERY CHART GOES THROUGH `frappe.get_list`, AND THAT IS THE WHOLE SECURITY MODEL. `get_list` routes to
`frappe/database/query.py`, which ANDs the `permission_query_conditions` hooks, every `User Permission`
row on every Link field, and the owner constraint, before a single row is counted. `frappe.get_all` sets
`ignore_permissions=True` unconditionally (`frappe/__init__.py:1386`) and `frappe.qb` builds its own SQL
and inherits nothing — so both are banned here and the ban is locked by
`tests/architecture/test_dashboard_has_one_query_door.py`. When a chart the declaration cannot express
comes along, the answer is that the chart is out of baseline, never a second query path.

GRAIN IS NOT WRITTEN HERE AND MUST NEVER BE. Vertical, group and programme are enforced natively by the
`User Permission` rows the site already carries against `CRM Lead`'s Link columns. Adding
`entitlement.entitled_grains()` on top would be a second brain AND a double filter. A chart declares its
list and says nothing about grain.

THE CARD AND THE LIST ARE THE SAME QUERY. Each datapoint carries the filter dict that reproduces it, built
from the same filters that produced the figure, so clicking a slice showing 91 opens a list of 91 rows.
That reconciliation is the property Phase 2 depends on, and it only holds because the drill filter is
DERIVED from the query rather than written a second time beside it.

Two things are done in Python because SQL cannot do them here: blank labels are named (`IFNULL` with a
literal default is not expressible — frappe reads the second argument as a fieldname), and the two blank
groups a column can have (NULL and the empty string) are collapsed into the one fact they are.

Plan: docs/plans/2026-07-31-dashboard-role-layouts-phase-1.md
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, now, today

# The filter controls a layout may expose. The layout controller asks this rather than restating it.
KNOWN_FILTERS = ("date_range", "vertical", "program", "user")

# Which SPA list a drill-down opens for each chartable list.
_ROUTES = {"CRM Lead": "Leads", "CRM Task": "Tasks"}

# A stored filter cannot hold "now", so these three stand for request time. No fourth — that is an expression language.
_TOKENS = {"__today__": today, "__now__": now, "__session_user__": lambda: frappe.session.user}

_GROUPED = ("donut", "bar")


def is_gated(chart, user=None):
	"""Whether this card's list really filters rows for this viewer, asked on EVERY read and not once at save.

	A registered Scope is an operator switch, so it can be turned off after a layout was written; a card over
	a switched-off list would count the whole site. A privileged viewer already sees everything, so nothing
	is hidden from them."""
	scope = visibility.SCOPED.get(chart["source_doctype"])
	return not scope or scope.armed() or visibility.is_privileged(user)


def run(chart, window=None, filters=None):
	"""One declared chart, executed. Returns the same envelope whatever the chart type is."""
	chart = frappe._dict(chart)
	query_filters = _filters(chart, window or {}, filters or {})
	payload = {
		"chart": chart.chart_name,
		"type": chart.chart_type,
		"label": chart.label,
		"subtitle": chart.subtitle or "",
		"value": 0,
		"points": [],
	}
	if chart.chart_type in _GROUPED:
		payload["points"] = _points(chart, _query(chart, query_filters, True), query_filters)
	# The figure is its OWN ungrouped query, never the sum of the points: the points are the top row_limit, so
	# summing them would print a total that disagrees with the list the same card's drill opens.
	rows = _query(chart, query_filters, False)
	payload["value"] = _measured(chart, rows[0].get("value") if rows else 0)
	if cint(chart.drill_enabled):
		payload["drill"] = _drill(chart, query_filters)
	return payload


def _filters(chart, window, dashboard_filters):
	"""What the card is about: its own declared rows, the viewer's narrowing, and the date window."""
	base = _substitute(frappe.parse_json(chart.base_filters), _snapshot())
	return {**base, **_narrowing(chart, dashboard_filters), **_window(chart, window)}


def _snapshot():
	"""The clock, read ONCE per card, so the figure and its drill filter cannot name different instants."""
	return {token: read() for token, read in _TOKENS.items()}


def _substitute(value, snap):
	if isinstance(value, str):
		return snap.get(value, value)
	if isinstance(value, list | tuple):
		return [_substitute(item, snap) for item in value]
	if isinstance(value, dict):
		return {key: _substitute(item, snap) for key, item in value.items()}
	return value


def _window(chart, window):
	"""The date range, on the column this chart declared as its own. A snapshot card ignores it by design."""
	if not (cint(chart.honours_date_range) and chart.date_field):
		return {}
	from_date, to_date = window.get("from_date"), window.get("to_date")
	if not (from_date and to_date):
		return {}
	return {chart.date_field: ["between", [from_date, to_date]]}


def _narrowing(chart, dashboard_filters):
	"""The viewer's own filters. Narrowing on top of the gate, NEVER the gate itself.

	A filter naming a column this chart's list does not have is skipped in silence — that is what lets one
	row of controls serve cards drawn from two different lists. The meta is asked; no doctype-to-field map
	is written down."""
	meta = frappe.get_meta(chart.source_doctype)
	narrowed = {}
	for name, fieldname in (("vertical", "custom_vertical"), ("program", "custom_current_program")):
		value = dashboard_filters.get(name)
		if value and meta.get_field(fieldname):
			narrowed[fieldname] = value
	chosen_user = dashboard_filters.get("user")
	if chosen_user:
		# A list with an owner column is filtered on it; anything else is filtered on the assignment.
		if meta.get_field("lead_owner"):
			narrowed["lead_owner"] = chosen_user
		else:
			narrowed["_assign"] = ["like", f"%{chosen_user}%"]
	return narrowed


def _query(chart, filters, grouped, user=None):
	"""THE one door. `get_list` and nothing else, so the row gate applies to a card as it does to a list."""
	kwargs = {"filters": filters, "fields": _fields(chart, grouped)}
	if grouped:
		kwargs["group_by"] = chart.group_by_field
		# Stated explicitly: an unstated direction flips to ASC under db_query_compat.
		kwargs["order_by"] = "value desc"
		kwargs["limit"] = cint(chart.row_limit) or 10
	if user:
		kwargs["user"] = user
	return frappe.get_list(chart.source_doctype, **kwargs)


def _fields(chart, grouped):
	"""Dict syntax, always — a string aggregate is refused outright by `_validate_select_field`."""
	fields = [{chart.aggregate: chart.aggregate_field or "name", "as": "value"}]
	if not grouped:
		return fields
	fields.insert(0, chart.group_by_field)
	return fields


def _labels(chart, rows):
	"""Display names, read as their OWN query and never reached through the link.

	Asking for `lead_owner.full_name` makes frappe JOIN the target table, and the row gate names its columns
	unqualified — so the join turns the gate's own `name IN (...)` ambiguous and MariaDB refuses the whole
	statement (1052). A privileged caller never sees it, because their gate is empty; every gated rep does."""
	if not chart.label_field or chart.label_field == chart.group_by_field:
		return {}
	link = frappe.get_meta(chart.source_doctype).get_field(chart.group_by_field)
	if not (link and link.options):
		return {}
	values = [row.get(chart.group_by_field) for row in rows if row.get(chart.group_by_field)]
	if not values:
		return {}
	titled = chart.label_field.split(".")[-1]
	# Unreadable target rows simply fall back to the raw value, which is already on screen either way.
	found = frappe.get_list(link.options, filters={"name": ["in", values]}, fields=["name", titled], limit=0)
	return {row["name"]: row.get(titled) for row in found if row.get(titled)}


def _points(chart, rows, filters):
	"""One datapoint per group, each carrying the filter that reproduces exactly it."""
	titles = _labels(chart, rows)
	points = []
	for row in rows:
		raw = row.get(chart.group_by_field)
		label = titles.get(raw, raw)
		point = {
			"label": _("Not set") if label in (None, "") else label,
			"raw": raw,
			"value": _measured(chart, row.get("value")),
		}
		if cint(chart.drill_enabled):
			# The RAW grouped value, never the label: `lead_owner.full_name` is a name, `lead_owner` filters.
			point["drill"] = _drill(chart, {**filters, chart.group_by_field: _term(raw)})
		points.append(point)
	return _merge_blanks(points)


def _term(raw):
	"""A blank group is NULL or the empty string, and only `is not set` returns both."""
	return ["is", "not set"] if raw in (None, "") else raw


def _merge_blanks(points):
	"""NULL and '' are two SQL groups and one fact — collapsed, or the slice and its drill disagree."""
	blanks = [point for point in points if point["raw"] in (None, "")]
	if len(blanks) < 2:
		return points
	merged = dict(blanks[0], value=sum(point["value"] for point in blanks))
	kept = [point for point in points if point["raw"] not in (None, "")] + [merged]
	return sorted(kept, key=lambda point: point["value"], reverse=True)


def _measured(chart, value):
	"""A count is a whole number; a sum or an average is not, and neither is JSON-safe as a Decimal."""
	return cint(value) if chart.aggregate == "COUNT" else flt(value)


def _drill(chart, filters):
	return {"doctype": chart.source_doctype, "route": _ROUTES.get(chart.source_doctype, ""), "filters": filters}
