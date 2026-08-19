# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""What a dashboard IS: the doctypes it spans, their fields, and the one door that reads them.

Every other module here asks this one. STRUCTURAL is what our code depends on, so the seed asserts it on
every migrate; PRESENTATION is the operator's wording, seeded once and never re-imposed.

LAYOUT is the crm SPA's own `CRM Dashboard`, extended by custom fields — there is no second one beside it.
"""

import frappe
from frappe import _
from frappe.utils import escape_html

CHART = "CRM Dashboard Chart"
LAYOUT = "CRM Dashboard"
PLACEMENT_DOCTYPE = "CRM Dashboard Placement"

STRUCTURAL = (
	"chart_type",
	"source_doctype",
	"aggregate",
	"aggregate_field",
	"group_by_field",
	"split_by",
	"time_bucket",
	"date_field",
	"honours_date_range",
	"base_filters",
	"row_limit",
	"drill_enabled",
)

PRESENTATION = ("chart_name", "label", "subtitle")

READ = PRESENTATION + STRUCTURAL

LAYOUT_FIELDS = ("name", "role", "title", "priority", "exposed_filters")

# A layout splits the same way a chart does. `title` is the operator's wording, seeded once and never
# re-imposed; the rest is what this app's code depends on and is re-asserted on every migrate. Without
# that split a layout created before a filter existed keeps offering none of them for ever — which is
# exactly how one UAT dashboard ended up with `exposed_filters = []` and no way to heal itself.
LAYOUT_STRUCTURAL = ("priority", "exposed_filters")

PLACEMENT = ("chart", "x", "y", "w", "h")

# Which of the schema's chart types the executor draws an axis for; `number` is the figure alone.
GROUPED = ("donut", "bar", "line", "stacked_bar", "heatmap")

# A heatmap is two dimensions crossed, so it is the one type that cannot be drawn from a single column.
CROSSED = "heatmap"

# The time bucket's vocabulary: what a card asks for, and the alias the bucketed column is grouped on.
NO_BUCKET = "none"
MONTH = "month"
BUCKET = "bucket"

# How many different values a column holds: frappe has no COUNT DISTINCT, so it is a NUMBER OF GROUPS.
DISTINCT = "DISTINCT"

# A split with two hundred values is a legend, not a chart; both the query bound and the pivot read this.
SERIES_LIMIT = 8

# What the kept series do not account for, so the bars always add up to the figure the card states.
OTHER = "__other__"

# The ceiling on a distinct count, so a high-cardinality column cannot pull every group into Python.
DISTINCT_LIMIT = 10000

CACHE_PREFIX = "tatva_connect:dashboard:"


def retire_cache():
	"""Every cached dashboard, dropped. Called when an operator edits a card or a layout, so a correction
	is on screen on the next load rather than a TTL later."""
	frappe.cache.delete_keys(CACHE_PREFIX)


def parsed(value, label, shape):
	"""One JSON field, read or refused. `shape` is dict or list."""
	try:
		value = frappe.parse_json(value or ("{}" if shape is dict else "[]"))
	except (TypeError, ValueError) as unreadable:
		frappe.throw(
			_("{0} is not valid JSON: {1}").format(label, escape_html(str(unreadable))),
			title=_("That JSON cannot be read"),
		)
	if not isinstance(value, shape):
		expected = _("an object") if shape is dict else _("a list")
		frappe.throw(
			_("{0} is {1}, not {2}.").format(label, expected, frappe.bold(type(value).__name__)),
			title=_("{0} has the wrong shape").format(label),
		)
	return value


def charts(names):
	"""Every named card that is enabled, in one read, keyed by name. Operator config, not user data."""
	names = list(names)
	if not names:
		return {}
	rows = frappe.get_list(
		CHART,
		filters={"name": ["in", names], "enabled": 1},
		fields=list(READ),
		limit=len(names),
		ignore_permissions=True,  # authz-ok: tier-c — reads the operator chart config the caller's own layout places
	)
	return {row["chart_name"]: row for row in rows}
