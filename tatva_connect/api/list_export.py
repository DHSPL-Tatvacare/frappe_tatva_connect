# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Export ships the rows the SCREEN showed — the one listing surface that is not served by the engine.

`ViewControls.vue`'s Export builds a direct URL to `frappe.desk.reportview.export_query`, which is outside
this app entirely: `reportview.validate_fields` throws on a fieldname `frappe.get_meta` does not carry, and
`DatabaseQuery` refuses the same name in `order_by` — while a derived FILTER is not understood at all, so
the best case was a rep silently receiving rows their list never showed.

The predicate is NOT re-expressed here and must never be re-expressed in the browser: a bucket is declared
once in `list_engine/fields.py`, and a JavaScript copy of it is a second brain that drifts the day the
declaration changes. This endpoint hands the screen's own three arguments through `ListRequest` — the SAME
translation the list ran through — and answers what `export_query` can be given:

    fields          the derived NAME dropped; the column does not exist, so it cannot be exported
    filters         the declared tuples, as `ListRequest.terms` already resolves them
    order_by        the declaration's sort proxy, or dropped when it declares none
    selected_items  the exact ids the screen composed, when the page's ORDER is what chose them

THE THIRD ARGUMENT IS THE ONE THAT WAS WRONG. `reportview` cannot express "order by bucket", so a screen
sorted by a derived field exported the first N of a differently-ordered set — the right COUNT of the wrong
ROWS, with no error. The page is composed here by the same walk the list runs and handed over as
`selected_items`, which `reportview.export_query:437` turns into `name in (…)`. The order is still lost,
which was decided and is documented; the rows are not.

`@me` IS PREPARED HERE, for every doctype. A rep exporting "assigned to me" was sending the literal token
to `reportview`, which has no session-user substitution — so the export silently matched nothing or the
wrong thing. `ListRequest.filters` mirrors native's own preparation, which is the one place that rule is
written, so the export now prepares filters exactly as the list did.

THE LINE is the engine's: when the request names no derived field, the caller's own arguments are handed
back in the caller's own SHAPE — a filters dict, not tuples — so an ordinary column's export is the URL it
was, with the one substitution native would have made.

Plan + the hardline rules: docs/plans/tasks-ui/2026-07-30-derived-fields-list-engine.md
"""

from contextlib import contextmanager

import frappe

from tatva_connect import exports
from tatva_connect.list_engine import derived
from tatva_connect.list_engine.engine import ListRequest

# What frappe's own `max_report_rows` field defaults to, for a site that has never set it.
DEFAULT_ROW_CAP = 100_000

# A cell beginning with any of these is EXECUTED by Excel, LibreOffice and Sheets, not displayed.
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def _as_text(value):
	"""Prefix a formula-leading cell with an apostrophe: every spreadsheet reads the rest as text."""
	if isinstance(value, str) and value.startswith(_FORMULA_LEADS):
		return "'" + value
	return value


@contextmanager
def _cells_are_never_formulas():
	"""Neutralise formula-leading cells in BOTH export formats, for the duration of one export.

	`title` on a lead or a task is free text a partner or a rep supplies, and nothing on the way in or the
	way out treats it as dangerous — frappe has no formula guard anywhere (`csvutils`, `xlsxutils` and
	`reportview` were all checked). So `=HYPERLINK("http://…"&A1)` in a name is inert in the CRM and runs
	the moment a manager opens the export. The payload carries no `<`, so the HTML sanitiser never sees it.

	THE WRAP IS THE SEAM, because `_export_query` has no other. It goes from `DatabaseQuery.execute` to
	finished bytes inside one function, and its only early exit hands back the encoded file. Sanitising
	bytes would mean re-parsing a CSV and giving up on xlsx entirely; the alternative is reimplementing the
	export here, which the module docstring forbids for good reason. Both builders are imported INSIDE
	`_export_query`, so rebinding them on their own modules is seen by that call and by nothing else, and
	the original is always restored.
	"""
	from frappe.desk import utils as desk_utils
	from frappe.utils import xlsxutils

	native_csv, native_xlsx = desk_utils.get_csv_bytes, xlsxutils.make_xlsx

	# *args/**kwargs deliberately: the wrapper touches the ROWS and nothing else, so an upstream signature change cannot silently drop an argument.
	def safe_csv(data, *args, **kwargs):
		return native_csv([[_as_text(v) for v in row] for row in data], *args, **kwargs)

	def safe_xlsx(data, *args, **kwargs):
		return native_xlsx([[_as_text(v) for v in row] for row in data], *args, **kwargs)

	desk_utils.get_csv_bytes, xlsxutils.make_xlsx = safe_csv, safe_xlsx
	try:
		yield
	finally:
		desk_utils.get_csv_bytes, xlsxutils.make_xlsx = native_csv, native_xlsx


@frappe.whitelist()
def export_query():
	"""Frappe's export, bounded by the operator's `max_report_rows`.

	Native sets `limit_page_length = None` and streams the whole result — that ONE assignment is all this
	override changes. Everything else is frappe's own: `get_form_params`, `pop_csv_params` and
	`_export_query` are called, never reimplemented, so a Desk report exports exactly as it always did.
	The background branch is frappe's alone and is delegated untouched, because this endpoint is
	site-wide — overriding it must not quietly remove a Desk feature the CRM happens not to use.

	The cap CAPS. An export over the ceiling returns the ceiling; it is never refused."""
	from frappe.desk import reportview
	from frappe.desk.utils import pop_csv_params

	form_params = reportview.get_form_params()
	form_params["limit_page_length"] = row_cap()
	form_params["as_list"] = True
	csv_params = pop_csv_params(form_params)
	# POPPED, not read — `get_form_params` leaves it in and it reached the query builder as an unknown keyword, 500ing every Desk export. Native pops it here for the same reason.
	# Native's own flag, our delivery: its background branch EMAILS the file; the tab that asked wants it back.
	if frappe.cint(form_params.pop("export_in_background", 0)):
		return exports.queue(
			"List", form_params.get("doctype"),
			"xlsx" if form_params.get("file_format_type") == "Excel" else "csv",
			{"form_params": dict(form_params), "csv_params": dict(csv_params)},
		)
	with _cells_are_never_formulas():
		return reportview._export_query(form_params, csv_params)


def produce_export(job, params, progress):
	"""The native-list producer for `tatva_connect.exports` — see that module for the returned shape.

	FRAPPE'S OWN `_export_query`, CALLED NOT REIMPLEMENTED. It already applies `can_export`, writes the
	`Access Log` row, runs the query and formats the file, and it answers `(title, extension, content)`
	when told not to populate the response. That is exactly a producer, which is why this is nine lines:
	the whole point of this module is that native's export is never re-expressed here.

	`progress` is not called. This producer is ONE query — there are no pages to report, and a callback
	invented to look busy would be a lie about what the worker is doing.
	"""
	from frappe.desk import reportview

	# The cap and the shape were set by `export_query` before this job was recorded; setting them a second
	# time here would be a second place that decides what an export may carry.
	form_params = frappe._dict(params.get("form_params") or {})
	csv_params = frappe._dict(params.get("csv_params") or {})
	with _cells_are_never_formulas():
		title, extension, content = reportview._export_query(form_params, csv_params, populate_response=False)
	return {"stem": title, "ext": extension, "content": content, "rows": None, "truncated": False}


@frappe.whitelist()
def export_args(
	doctype: str,
	fields: str | None = None,
	filters: str | None = None,
	order_by: str = "",
	page_length: int | None = None,
	export_all: int = 0,
):
	"""The arguments `export_query` must be given to return the rows the rep is looking at."""
	asked = frappe.parse_json(fields or "[]") or []
	declared = {f.fieldname for f in derived.for_doctype(doctype)}
	request = ListRequest({"doctype": doctype, "filters": filters, "order_by": order_by})
	args = {
		"fields": [f for f in asked if f not in declared],
		"filters": dict(request.filters),
		"order_by": order_by or "",
	}
	if not request.named:
		return args

	# Flat, derived-free filters; the narrowing travels as ids instead — `ListRequest.names` says why.
	args["filters"] = dict(request.plain)
	args["order_by"] = request.for_native().get("order_by") or ""
	args["selected_items"] = request.names(_wanted(page_length, export_all))
	return args


def _wanted(page_length, export_all):
	"""How many rows the export may carry: the operator's ceiling, and the rep's own page when they asked
	for a page rather than for everything."""
	cap = row_cap()
	if frappe.cint(export_all) or not frappe.cint(page_length):
		return cap
	return min(cap, frappe.cint(page_length))


def row_cap():
	"""The operator's ceiling on one export, read from frappe's OWN System Settings field.

	`max_report_rows` is declared by frappe and enforced NOWHERE on the server — its only reader in the
	whole framework is one line of report-viewer JavaScript (`query_report.js:1075`). The field already
	exists, an operator already knows where it lives, and it already says what it means, so honouring it
	here invents no setting and hardcodes no number. Its own default stands in when a site has never set it."""
	return frappe.cint(frappe.get_system_settings("max_report_rows")) or DEFAULT_ROW_CAP
