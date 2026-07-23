# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The one whitelisted endpoint the spotlight modal calls. It runs the native FTS5 search (already
grain-scoped by `get_search_filters`) and shapes each hit for the frontend — which filters, ranks and
decides nothing. All visibility is server-side and fail-closed."""
import frappe

from tatva_connect.search.index import TAB, CRMLeadSearch

_MIN = 3


@frappe.whitelist()
def search(query, type=None, limit=20):
	"""Return `{results, total}`. Empty for a short/blank query or while search is dormant — never a
	round-trip that scans. `type` is an optional doctype facet (CRM Lead / FCRM Note / ...)."""
	query = (query or "").strip()
	if len(query) < _MIN:
		return {"results": [], "total": 0}

	engine = CRMLeadSearch()
	if not engine.is_search_enabled() or not engine.index_exists():
		return {"results": [], "total": 0}

	filters = {"doctype": type} if type else None
	res = engine.search(query, filters=filters) or {}

	results = [_shape(r) for r in res.get("results", [])[: int(limit)]]
	total = res.get("summary", {}).get("total_matches", len(results))
	return {"results": results, "total": total}


def _shape(r):
	"""Flatten a framework hit into what the row + click-through need. A File also carries its `file_url`
	so the modal can open the bytes with window.open — resolved here (few hits), never indexed."""
	dt = r.get("doctype")
	hit = {
		"doctype": dt,
		"name": r.get("name"),
		"lead": r.get("lead") or (r.get("name") if dt == "CRM Lead" else None),
		"tab": TAB.get(dt),
		"title": r.get("title"),
		"snippet": r.get("content"),
		"phone": r.get("phone"),
		"vertical": r.get("vertical"),
		"group": r.get("group"),
		"owner": r.get("owner"),
		"score": r.get("score"),
	}
	if dt == "File":
		hit["file_url"] = frappe.db.get_value("File", r.get("name"), "file_url")
	return hit
