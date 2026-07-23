# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The one whitelisted endpoint the spotlight modal calls; the frontend renders its shape and decides nothing."""
import frappe

from tatva_connect.search.index import TAB, CRMLeadSearch

_MIN = 3


@frappe.whitelist()
def search(query, type=None, limit=20):
	# Empty for a short/blank query or while dormant; `type` is an optional doctype facet.
	query = (query or "").strip()
	if len(query) < _MIN:
		return {"results": [], "total": 0}

	engine = CRMLeadSearch()
	if not engine.is_search_enabled() or not engine.index_exists():
		return {"results": [], "total": 0}

	filters = {"doctype": type} if type else None
	res = engine.search(query, filters=filters) or {}

	# Leads first, then notes, then files; within a group, by relevance.
	results = [_shape(r) for r in res.get("results", [])]
	results.sort(key=lambda h: (_RANK.get(h["doctype"], 9), -(h.get("score") or 0)))
	total = res.get("summary", {}).get("total_matches", len(results))
	return {"results": results[: int(limit)], "total": total}


_RANK = {"CRM Lead": 0, "FCRM Note": 1, "File": 2}


def _shape(r):
	# Flatten a framework hit for the row + click-through; a File also carries its file_url for window.open.
	dt = r.get("doctype")
	hit = {
		"doctype": dt,
		"name": r.get("name"),
		"lead": r.get("lead") or (r.get("name") if dt == "CRM Lead" else None),
		"tab": TAB.get(dt),
		"title": r.get("title"),
		"snippet": r.get("content"),
		"phone": r.get("phone"),
		"status": r.get("status"),
		"vertical": r.get("vertical"),
		"group": r.get("lead_group"),
		"assignee": r.get("assignee"),
		"score": r.get("score"),
	}
	if dt == "File":
		hit["file_url"] = frappe.db.get_value("File", r.get("name"), "file_url")
	return hit


@frappe.whitelist()
def rebuild_index():
	# Force a full background rebuild — the Rebuild button on the toggle's form; System Manager only, deduplicated.
	frappe.only_for("System Manager")
	from tatva_connect.search.activation import enqueue_build

	enqueue_build(force=True)
	return {"queued": True}
