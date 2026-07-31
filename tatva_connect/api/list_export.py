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

    fields     the derived NAME dropped; the column does not exist, so it cannot be exported
    filters    the declared tuples, as `ListRequest.terms` already resolves them
    order_by   the declaration's sort proxy, or dropped when it declares none

THE LINE is the engine's: when the request names no derived field, the caller's own arguments are handed
straight back, so an ordinary column's export is byte for byte the URL it was before this file existed.

Plan + the hardline rules: docs/plans/tasks-ui/2026-07-30-derived-fields-list-engine.md
"""

import frappe

from tatva_connect.list_engine import derived
from tatva_connect.list_engine.engine import ListRequest


@frappe.whitelist()
def export_args(doctype: str, fields: str | None = None, filters: str | None = None, order_by: str = ""):
	"""The three arguments `export_query` must be given to return the rows the rep is looking at."""
	asked = frappe.parse_json(fields or "[]") or []
	declared = {f.fieldname for f in derived.for_doctype(doctype)}
	args = {
		"fields": [f for f in asked if f not in declared],
		"filters": frappe.parse_json(filters or "{}") or {},
		"order_by": order_by or "",
	}
	if not declared:
		return args

	request = ListRequest({"doctype": doctype, "filters": filters, "order_by": order_by})
	if request.named:
		args["filters"] = request.terms
		args["order_by"] = request.for_native().get("order_by") or ""
	return args
