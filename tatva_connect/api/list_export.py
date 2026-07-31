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

import frappe

from tatva_connect.list_engine import derived
from tatva_connect.list_engine.engine import ListRequest


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

	args["filters"] = request.terms
	args["order_by"] = request.for_native().get("order_by") or ""
	# Exporting EVERYTHING needs no page, and thousands of ids in a query string is how a URL 414s.
	if not frappe.cint(export_all) and frappe.cint(page_length):
		names = request.page_names(frappe.cint(page_length))
		if names is not None:
			args["selected_items"] = names
	return args
