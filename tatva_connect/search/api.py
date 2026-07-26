# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The one whitelisted endpoint the spotlight modal calls; the frontend renders its shape and decides nothing."""
import frappe
from frappe.search.sqlite_search import MAX_SEARCH_RESULTS

from tatva_connect.automation.settings import is_enabled
from tatva_connect.search import vocabulary
from tatva_connect.search.index import TAB, CRMLeadSearch, matched_identifier

_MIN = 3

# The dormant toggle for the query split — off, the vocabulary is never consulted and the response is today's.
SPLIT_TOGGLE = "Search::Query::vocabulary"


@frappe.whitelist()
def search(query, type=None, limit=20):
	# Empty for a short/blank query or while dormant; `type` is an optional doctype facet.
	query = (query or "").strip()
	engine = CRMLeadSearch()
	status = _status(engine)
	if len(query) < _MIN:
		return {"results": [], "total": 0, "status": status}

	if not engine.is_search_enabled() or not engine.index_exists():
		return {"results": [], "total": 0, "status": status}

	filters = {"doctype": type} if type else None
	text, understood = _split(query) if is_enabled(SPLIT_TOGGLE) else (query, None)
	if understood:
		filters = {**(filters or {}), **{f["column"]: f["value"] for f in understood["filters"]}}

	res = engine.search(text, filters=filters) or {}

	# Order is the engine's now — one ranking, in get_scoring_pipeline, where the doctype preference lives.
	# The typed query, not the text lane's remainder, is what decides whether an ID was punched in.
	results = [_shape(r, query) for r in res.get("results", [])]
	total = res.get("summary", {}).get("total_matches", len(results))
	# The framework truncates to MAX_SEARCH_RESULTS, so a plateaued count is a floor and the UI must say so.
	out = {
		"results": results[: int(limit)],
		"total": total,
		"status": status,
		"total_capped": total >= MAX_SEARCH_RESULTS,
	}
	if understood:
		out["understood"] = understood
	return out


def _split(query):
	"""Recognised words become index filters, everything else stays text — the two lanes, decided by the vocabulary."""
	reading = vocabulary.match(query)
	labels = vocabulary.labels()
	filters, spare = {}, []
	for column, value in reading.matched:
		# ONE filter per column: two values for the same column are an AND that returns zero rows, so the first
		# reading wins and the rest goes back to the text lane rather than emptying the result set.
		if column in filters:
			spare.append(vocabulary.normalise(value))
		else:
			filters[column] = value
	text = " ".join([*reading.leftover, *spare])
	# A filter NARROWS a text search; it never replaces one (FTS5 has no match-everything), so a query with
	# nothing left for the text lane is answered exactly as today — no filters, no interpretation.
	if not filters or not text:
		return query, None
	shown = [{"column": column, "label": labels.get(column, column), "value": value} for column, value in filters.items()]
	return text, {"filters": shown, "text": text}


def _status(engine):
	# Why an empty list is empty — dormant, not yet built, or a real no-match; the engine's own predicates decide it, nothing here re-derives them.
	if not engine.is_search_enabled():
		return "disabled"
	if not engine.index_exists() or not engine._is_indexing_complete():
		return "building"
	return "ready"


def _shape(r, query):
	# Flatten a framework hit into the row's four fixed slots (phone, product line, group, program) plus what it
	# needs to open. A unique ID reaches the response in ONE place only — `ident`, and only when it was typed;
	# `lead` and `file_url` are navigation, not display, and file_url is indexed metadata so it costs no read.
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
		"program": r.get("program"),
		"assignee": r.get("assignee"),
		"score": r.get("score"),
	}
	if dt == "File":
		hit["file_url"] = r.get("file_url")
	ident = matched_identifier(r, query)
	if ident:
		hit["ident"] = ident
	return hit


@frappe.whitelist()
def rebuild_index():
	# Force a full background rebuild — the Rebuild button on the toggle's form; System Manager only, deduplicated.
	frappe.only_for("System Manager")
	from tatva_connect.search.activation import enqueue_build

	enqueue_build(force=True)
	return {"queued": True}
