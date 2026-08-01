# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""A declaration in, a card out. The one place in this module that touches data.

Every chart goes through `frappe.get_list`, and that is the whole security model: it ANDs the
permission_query_conditions hooks, the User Permission rows on every Link field, and the owner constraint.
`get_all` and `frappe.qb` inherit none of that and are banned here, locked by
tests/architecture/test_dashboard_has_one_query_door.py.

Grain is therefore never written here — the site's own User Permission rows already enforce it.

A card may declare a SECOND dimension (`split_by`) and a time bucket, and neither changes the shape a
single-dimension card has always had: `points` is still the axis and is byte for byte what it was, and
`series` is added only where a split was declared. Both dimensions come out of the SAME grouped call.

A derived field is not a column, so it cannot be named in `group_by` — but each of its buckets IS a frappe
filter list, so a card grouped by one is one gated count per bucket through the very same door.
"""

import calendar

import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, getdate, now, nowdate

from tatva_connect import tokens
from tatva_connect.access import visibility
from tatva_connect.dashboard import declaration
from tatva_connect.list_engine import derived
from tatva_connect.taxonomy import labels

_ROUTES = {"CRM Lead": "Leads", "CRM Task": "Tasks"}

# This surface's own vocabulary; the walker that applies it is shared with the list engine (tokens.py).
_TOKENS = {"__TODAY__": nowdate, "__NOW__": now, "__SESSION_USER__": lambda: frappe.session.user}

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
	if chart.chart_type in declaration.GROUPED:
		payload.update(_breakdown(chart, query_filters, _calendar(chart, window or {})))
	payload["value"] = _figure(chart, query_filters)
	if cint(chart.drill_enabled):
		payload["drill"] = _drill(chart, query_filters)
	return payload


def _figure(chart, filters):
	"""The card's own number, from its own ungrouped query — never the sum of the points, which are capped
	at row_limit and would disagree with the list this card's drill opens."""
	if chart.aggregate == declaration.DISTINCT:
		return _distinct(chart, filters)
	rows = _query(chart, filters, False)
	return _measured(chart, rows[0].get("value") if rows else 0)


def _distinct(chart, filters):
	"""How many DIFFERENT values a column holds, as the number of groups a group-by returns.

	Frappe has no COUNT DISTINCT and this needs none: grouping by the column is the same question, asked
	through the same gated door. Blank is not a value — the rest of this module reads NULL and '' as one
	non-answer, and counting them as two people is the module disagreeing with itself."""
	return len(_groups(chart, {**filters, chart.aggregate_field: ["is", "set"]},
	                   chart.aggregate_field, declaration.DISTINCT_LIMIT))


def _breakdown(chart, filters, calendar_months):
	"""What a grouped card is broken down into: always `points`, and `series` where a split was declared."""
	field = derived.get(chart.source_doctype, chart.group_by_field) if chart.group_by_field else None
	if field:
		return {"points": _derived_points(chart, field, filters)}
	if chart.split_by:
		return _pivot(chart, filters, calendar_months)
	return {"points": _points(chart, _query(chart, filters, True), filters, calendar_months)}


def _derived_points(chart, field, filters):
	"""A card grouped by a derived field, as ONE gated count per bucket, in the order the field declares.

	`row_limit` is not read: a derived field's buckets are the whole of what it can be, they are few, and
	dropping one would leave a slice of the card silently missing rather than merely capped."""
	snap = derived.snapshot()
	conditions = _conditions(chart, filters)
	points = []
	for bucket in field.buckets:
		# The drill names the derived FIELD, never its tuples: a flattened bucket loses its second term.
		points.append(
			_point(
				chart,
				bucket.value,
				{},
				_count(chart, [*conditions, *derived.resolve(field, bucket, snap)]),
				filters,
				{field.fieldname: bucket.value},
			)
		)
	return points


def _pivot(chart, filters, calendar_months):
	"""Two grouped columns turned into one series per split value, in THREE gated queries.

	The series are chosen first by their own totals; the AXIS is the card's own totals asked without the
	split; the cross is then narrowed to the chosen series. The axis query is what makes the picture agree
	with the number — read off the cross alone, an axis value whose rows all fall outside the kept series
	has no row at all, so a whole month vanished from the chart while the card's figure still counted it.

	What the kept series do not account for becomes ONE more series, `Other`, so the bars always add up to
	the figure the card states. It is an ordinary series and travels the ordinary path."""
	kept = _top_series(chart, filters)
	if not kept:
		return {"points": [], "series": []}
	dimension = _dimension(chart)
	axis_rows = _query(frappe._dict({**chart, "split_by": ""}), filters, True)
	rows = _query(chart, filters, True, len(kept), _only_these_series(chart, kept))
	titles = _labels(chart, [row.get(dimension) for row in axis_rows], dimension)
	splits = _labels(chart, kept, chart.split_by)

	axis, totals = [], {}
	for row in axis_rows:
		x = _blank(row.get(dimension))
		if x not in totals:
			axis.append(x)
			totals[x] = 0
		totals[x] += _measured(chart, row.get("value"))

	cells = {}
	for row in rows:
		key = (_blank(row.get(chart.split_by)), _blank(row.get(dimension)))
		cells[key] = cells.get(key, 0) + _measured(chart, row.get("value"))

	axis = _ordered(axis, calendar_months)
	drawn = kept + _other(cells, totals, axis, kept, splits)
	return {
		"points": [
			_point(chart, x, titles, totals[x], filters, _narrows(dimension, x), calendar_months) for x in axis
		],
		"series": [
			_series(chart, split, splits, axis, cells, titles, filters, dimension, calendar_months)
			for split in drawn
		],
	}


def _other(cells, totals, axis, kept, splits):
	"""Everything the kept series do not account for, as one more series — or nothing when they account for
	it all. Written into `cells` so it draws exactly as any other series does."""
	remainder = {x: totals[x] - sum(cells.get((one, x), 0) for one in kept) for x in axis}
	if not any(value > 0 for value in remainder.values()):
		return []
	for x in axis:
		cells[(declaration.OTHER, x)] = remainder[x]
	splits[declaration.OTHER] = _("Other")
	return [declaration.OTHER]


def _ordered(axis, calendar_months):
	"""A month axis reads in the window's order, not MONTH()'s 1-12 — a Sep-to-Aug window starts at Sep."""
	if not calendar_months:
		return axis
	sequence = list(calendar_months)
	return sorted(axis, key=lambda x: sequence.index(cint(x)) if cint(x) in sequence else len(sequence))


def _blank(raw):
	"""NULL and '' are two SQL groups and one fact. Every axis, series and count reads blank through here,
	so no two of them can disagree about what blank is."""
	return "" if raw in (None, "") else raw


def _calendar(chart, window):
	"""The months the window spans, oldest first, as {month_number: "Aug 2025"}.

	MONTH() answers 1-12 with the year folded away, so a Sep-to-Aug window would sort as Jan..Dec and read
	two different years as one axis. The window is what puts the year back, and it is the only thing that
	can: the last twelve months of it are taken, because beyond that the month numbers repeat and no
	mapping exists."""
	if chart.time_bucket != declaration.MONTH or not (window.get("from_date") and window.get("to_date")):
		return {}
	end = getdate(window["to_date"]).replace(day=1)
	months = {}
	for back in range(11, -1, -1):
		when = add_months(end, -back)
		if getdate(when) >= getdate(window["from_date"]).replace(day=1):
			months[when.month] = f"{calendar.month_abbr[when.month]} {when.year}"
	return months


def _only_these_series(chart, kept):
	"""The cross narrowed to the series this card draws, as `or_filters` — frappe ANDs this whole group onto
	`filters`. It is a group and not one condition because `in` cannot match NULL, so a blank series needs
	its own `is not set` term beside the named ones."""
	terms = [[chart.split_by, "is", "not set"]] if "" in kept else []
	named = [one for one in kept if one != ""]
	if named:
		terms.append([chart.split_by, "in", named])
	return terms


def _series(chart, split, splits, axis, cells, titles, filters, dimension, calendar_months=None):
	"""One series: a cell for EVERY axis value, so a hole in the data is a zero and never a missing bar."""
	return {
		"name": _("Not set") if split in (None, "") else splits.get(split, split),
		"raw": split,
		"points": [
			_point(chart, x, titles, cells.get((split, x), 0), filters, _crossed(chart, dimension, x, split), calendar_months)
			for x in axis
		],
	}


def _top_series(chart, filters):
	"""The split values this card draws, from their OWN gated query: largest by the card's own measure.

	Asked first and separately, because the cross that follows can then be narrowed to exactly these — which
	is what makes its row cap a bound rather than a hope."""
	rows = _groups(chart, filters, chart.split_by, declaration.SERIES_LIMIT)
	return [_blank(row.get(chart.split_by)) for row in rows]


def _groups(chart, filters, column, limit):
	"""What DIFFERENT values one column holds, largest by the card's own measure. The shape both the series
	picker and the distinct count need, asked once here so the two cannot drift apart."""
	return frappe.get_list(
		chart.source_doctype,
		filters=filters,
		fields=[column, _measure(chart)],
		group_by=column,
		order_by="value desc",
		limit=limit,
	)


def _dimension(chart):
	"""The key a grouped row carries its axis value under: the bucket alias, or the declared column."""
	return declaration.BUCKET if chart.time_bucket == declaration.MONTH else chart.group_by_field


def _narrows(dimension, raw):
	"""What a click on one axis value adds to the card's filters, or None where a click cannot filter it.

	A month bucket is a select alias no column takes, and MONTH answers 1-12 with the year folded away, so
	no date range expresses it either — such a point carries no drill rather than one that would disagree."""
	return None if dimension == declaration.BUCKET else {dimension: _term(raw)}


def _crossed(chart, dimension, raw, split):
	"""What a click on one CELL adds: both dimensions. Nothing where the axis cannot be filtered, and
	nothing for `Other` — "everything else" is not a value any filter can name."""
	narrowing = _narrows(dimension, raw)
	if narrowing is None or split == declaration.OTHER:
		return None
	return {**narrowing, chart.split_by: _term(split)}


def _conditions(chart, filters):
	"""A filter DICT as the list form `get_list` also takes, so a derived bucket's tuples can be ANDed on.

	The two-element reading is frappe's own dict semantics (`[operator, value]`) and is restated nowhere
	else — this is the one place a dashboard filter has to be expressed as conditions instead."""
	stated = []
	for fieldname, condition in filters.items():
		operator, value = (
			condition if isinstance(condition, list | tuple) and len(condition) == 2 else ("=", condition)
		)
		stated.append([chart.source_doctype, fieldname, operator, value])
	return stated


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


def _query(chart, filters, grouped, series=1, or_filters=None):
	"""The card's own read. `series` is how many split values the cross was narrowed to, so the row cap is
	that many axis runs and never an arithmetic guess at a product nobody bounded."""
	kwargs = {"filters": filters, "fields": _fields(chart, grouped)}
	if or_filters:
		kwargs["or_filters"] = or_filters
	if grouped:
		# The alias and not the expression: MariaDB groups on a select alias, and the pivot reads that key.
		kwargs["group_by"] = ", ".join([_dimension(chart), *([chart.split_by] if chart.split_by else [])])
		# Stated in both directions: unstated flips to ASC under db_query_compat. A month reads in order.
		bucketed = chart.time_bucket == declaration.MONTH
		kwargs["order_by"] = f"{declaration.BUCKET} asc" if bucketed else "value desc"
		kwargs["limit"] = (cint(chart.row_limit) or 10) * series
	return frappe.get_list(chart.source_doctype, **kwargs)


def _count(chart, conditions):
	rows = frappe.get_list(chart.source_doctype, filters=conditions, fields=[_measure(chart)])
	return _measured(chart, rows[0].get("value") if rows else 0)


def _measure(chart):
	"""The card's figure as `get_list` selects it. Dict syntax always — a string aggregate is refused by
	frappe's own _validate_select_field, and DISTINCT is this app's word for "how many groups" rather than a
	function frappe has, so the measure underneath one is the count it is grouping."""
	aggregate = "COUNT" if chart.aggregate == declaration.DISTINCT else chart.aggregate
	return {aggregate: chart.aggregate_field or "*", "as": "value"}


def _fields(chart, grouped):
	if not grouped:
		return [_measure(chart)]
	# The dict form is frappe's own; the alias is what the group-by and the pivot read it back under.
	axis = (
		{"MONTH": chart.date_field, "as": declaration.BUCKET}
		if chart.time_bucket == declaration.MONTH
		else chart.group_by_field
	)
	return [axis, *([chart.split_by] if chart.split_by else []), _measure(chart)]


def _labels(chart, values, column):
	"""What a Link column's values READ as, so a cell shows the display label and never the composite key.

	`taxonomy.labels` owns the title_field read for this whole app — this asks it, so there is ONE
	implementation of it and not two. Nothing is declared per card: frappe already knows a doctype's title
	field, so a User reads as a full name and a task type as its type name."""
	link = frappe.get_meta(chart.source_doctype).get_field(column)
	if not (link and link.fieldtype == "Link" and link.options):
		return {}
	return {one: labels.label(one, link.options) for one in dict.fromkeys(values) if one}


def _point(chart, raw, titles, value, filters, narrowing, calendar_months=None):
	"""One datapoint: what it reads as, what it really holds, its figure, and the list a click opens.

	`narrowing` is what the click adds to the card's own filters, and `None` means this point cannot be
	drilled at all — carried as no `drill` key, exactly as a card that declares itself undrillable is."""
	if _dimension(chart) == declaration.BUCKET:
		# The window names the year; without one there is still a month to name, and never a bare number.
		label = (calendar_months or {}).get(cint(raw)) or calendar.month_abbr[cint(raw)]
	else:
		label = titles.get(raw, raw)
	point = {
		"label": _("Not set") if label in (None, "") else label,
		"raw": raw,
		"value": value,
	}
	if narrowing is not None and cint(chart.drill_enabled):
		# The raw grouped value, never the label: `lead_owner.full_name` is a name, `lead_owner` filters.
		point["drill"] = _drill(chart, {**filters, **narrowing})
	return point


def _points(chart, rows, filters, calendar_months):
	"""One axis, blanks folded together as they are on a split card: NULL and '' are one slice, not two."""
	dimension = _dimension(chart)
	titles = _labels(chart, [row.get(dimension) for row in rows], dimension)
	totals, order = {}, []
	for row in rows:
		x = _blank(row.get(dimension))
		if x not in totals:
			order.append(x)
			totals[x] = 0
		totals[x] += _measured(chart, row.get("value"))
	# A month reads in the window's order; every other breakdown reads largest first, as it always has.
	if dimension == declaration.BUCKET:
		order = _ordered(order, calendar_months)
	else:
		order.sort(key=lambda x: totals[x], reverse=True)
	return [
		_point(chart, x, titles, totals[x], filters, _narrows(dimension, x), calendar_months) for x in order
	]


def _term(raw):
	# Only `is not set` returns both of the two SQL groups blank splits into.
	return ["is", "not set"] if _blank(raw) == "" else raw


def _measured(chart, value):
	return cint(value) if chart.aggregate == "COUNT" else flt(value)


def _drill(chart, filters):
	return {"doctype": chart.source_doctype, "route": _ROUTES[chart.source_doctype], "filters": filters}
