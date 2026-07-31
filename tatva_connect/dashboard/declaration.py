# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""What a dashboard IS: the two doctypes, their fields, and the one door that reads them.

Every other module here asks this one. STRUCTURAL is what our code depends on, so the seed asserts it on
every migrate; PRESENTATION is the operator's wording, seeded once and never re-imposed.
"""

import frappe
from frappe import _
from frappe.utils import escape_html

CHART = "CRM Dashboard Chart"
LAYOUT = "CRM Dashboard Layout"

STRUCTURAL = (
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

PRESENTATION = ("chart_name", "label", "subtitle")

READ = PRESENTATION + STRUCTURAL

LAYOUT_FIELDS = ("name", "role", "title", "priority", "layout", "exposed_filters")

PLACEMENT = ("x", "y", "w", "h")

CACHE_PREFIX = "tatva_connect:dashboard:"


def retire_cache():
	"""Every cached dashboard, dropped. Called when an operator edits a card or a layout, so a correction
	is on screen on the next load rather than a TTL later."""
	frappe.cache().delete_keys(CACHE_PREFIX)


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
