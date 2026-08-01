# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""A declaration in, a card out. The one place in this module that touches data.

Every chart goes through `frappe.get_list`, and that is the whole security model: it ANDs the
permission_query_conditions hooks, the User Permission rows on every Link field, and the owner constraint.
`get_all` and `frappe.qb` inherit none of that and are banned here, locked by
tests/architecture/test_dashboard_has_one_query_door.py.

Grain is therefore never written here — the site's own User Permission rows already enforce it.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, now, nowdate

from tatva_connect import tokens
from tatva_connect.access import visibility

_ROUTES = {"CRM Lead": "Leads", "CRM Task": "Tasks"}

# This surface's own vocabulary; the walker that applies it is shared with the list engine (tokens.py).
_TOKENS = {"__TODAY__": nowdate, "__NOW__": now, "__SESSION_USER__": lambda: frappe.session.user}

_GROUPED = ("donut", "bar")

# A filter IS a fieldname. These two are not: a period is no column at all, and `user` means a different column per list.
DATE_RANGE = "date_range"
_ALIASES = {"user": ("lead_owner", "assigned_to")}

# Wording for the two that have no field of their own; every other label is the field's own.
_LABELS = {DATE_RANGE: "Period", "user": "Sales User"}


def _columns(name):
	return _ALIASES.get(name, (name,))


def exposed_controls(names):
	"""What the page draws above the cards: each control's name, its wording, and the lead column it narrows.
	The browser renders what it is handed and holds no map of its own."""
	meta = frappe.get_meta("CRM Lead")
	controls = []
	for name in names:
		field = meta.get_field(name) if name not in _LABELS else None
		controls.append({
			"name": name,
			"label": _(field.label if field else _LABELS.get(name, name)),
			"column": field.fieldname if field else None,
		})
	return controls


def is_gated(chart, user=None):
	"""Whether this card's list really filters rows for this viewer. Asked on every read, not once at save:
	a Scope is an operator switch and can be turned off long after a layout was written."""
	scope = visibility.SCOPED.get(chart["source_doctype"])
	return not scope or scope.armed() or visibility.is_privileged(user)


def envelope(chart, error=False):
	"""The card shape, declared once, so a failed card and a working one read alike to the browser."""
	chart = frappe._dict(chart)
	return {
		"chart": chart.chart_name,
		"type": chart.chart_type,
		"label": chart.label,
		"subtitle": chart.subtitle or "",
		"value": 0,
		"points": [],
		**({"error": True} if error else {}),
	}


def run(chart, window=None, filters=None):
	chart = frappe._dict(chart)
	query_filters = _filters(chart, window or {}, filters or {})
	payload = envelope(chart)
	if chart.chart_type in _GROUPED:
		payload["points"] = _points(chart, _query(chart, query_filters, True), query_filters)
	# Its own ungrouped query, never the sum of the points: those are capped at row_limit and would disagree with the list this card's drill opens.
	rows = _query(chart, query_filters, False)
	payload["value"] = _measured(chart, rows[0].get("value") if rows else 0)
	if cint(chart.drill_enabled):
		payload["drill"] = _drill(chart, query_filters)
	return payload


def _filters(chart, window, chosen):
	base = tokens.substitute(frappe.parse_json(chart.base_filters), tokens.snapshot(_TOKENS))
	return {**base, **_narrowing(chart, chosen), **_window(chart, window)}


def _window(chart, window):
	if not (cint(chart.honours_date_range) and chart.date_field):
		return {}
	from_date, to_date = window.get("from_date"), window.get("to_date")
	if not (from_date and to_date):
		return {}
	return {chart.date_field: ["between", [from_date, to_date]]}


def _narrowing(chart, chosen):
	"""A chosen filter narrows the first of its columns this list actually has; a list with none is skipped,
	which is what lets one row of controls serve cards drawn from two different lists."""
	meta = frappe.get_meta(chart.source_doctype)
	narrowed = {}
	for name, value in chosen.items():
		if not value or name == DATE_RANGE:
			continue
		column = next((c for c in _columns(name) if meta.get_field(c)), None)
		if column:
			narrowed[column] = value
	return narrowed


def _query(chart, filters, grouped):
	kwargs = {"filters": filters, "fields": _fields(chart, grouped)}
	if grouped:
		kwargs["group_by"] = chart.group_by_field
		# Direction stated: unstated flips to ASC under db_query_compat.
		kwargs["order_by"] = "value desc"
		kwargs["limit"] = cint(chart.row_limit) or 10
	return frappe.get_list(chart.source_doctype, **kwargs)


def _fields(chart, grouped):
	# Dict syntax always — a string aggregate is refused by frappe's own _validate_select_field.
	measure = {chart.aggregate: chart.aggregate_field or "*", "as": "value"}
	return [chart.group_by_field, measure] if grouped else [measure]


def _labels(chart, rows):
	"""Display names as their own query, not through `lead_owner.full_name`.

	A traversed field is permission-checked against the LINK TARGET (query.py check_filter_field_permission),
	so a viewer without read on User loses the whole card rather than just the labels. Read separately, an
	unreadable target costs labels only — the raw value is already on screen. Batched, because frappe's own
	chart does this one row at a time (desk/doctype/dashboard_chart/dashboard_chart.py:289)."""
	if not chart.label_field or chart.label_field == chart.group_by_field:
		return {}
	link = frappe.get_meta(chart.source_doctype).get_field(chart.group_by_field)
	values = [row[chart.group_by_field] for row in rows if row.get(chart.group_by_field)]
	if not (link and link.options and values):
		return {}
	titled = chart.label_field.split(".")[-1]
	found = frappe.get_list(link.options, filters={"name": ["in", values]}, fields=["name", titled], limit=0)
	return {row["name"]: row[titled] for row in found if row.get(titled)}


def _points(chart, rows, filters):
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
			# The raw grouped value, never the label: `lead_owner.full_name` is a name, `lead_owner` filters.
			point["drill"] = _drill(chart, {**filters, chart.group_by_field: _term(raw)})
		points.append(point)
	return _merge_blanks(points)


def _term(raw):
	# NULL and '' are two SQL groups and one fact; only `is not set` returns both.
	return ["is", "not set"] if raw in (None, "") else raw


def _merge_blanks(points):
	blanks = [point for point in points if point["raw"] in (None, "")]
	if len(blanks) < 2:
		return points
	merged = dict(blanks[0], value=sum(point["value"] for point in blanks))
	kept = [point for point in points if point["raw"] not in (None, "")] + [merged]
	return sorted(kept, key=lambda point: point["value"], reverse=True)


def _measured(chart, value):
	return cint(value) if chart.aggregate == "COUNT" else flt(value)


def _drill(chart, filters):
	return {"doctype": chart.source_doctype, "route": _ROUTES[chart.source_doctype], "filters": filters}
