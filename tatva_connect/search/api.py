# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The one whitelisted endpoint the spotlight modal calls; the frontend renders its shape and decides nothing."""
import re
from collections import Counter

import frappe
from frappe.search.sqlite_search import MAX_SEARCH_RESULTS, MIN_WORD_LENGTH
from frappe.utils import cint

from tatva_connect.automation.settings import is_enabled
from tatva_connect.search import vocabulary
from tatva_connect.search.index import TAB, CRMLeadSearch, matched_identifier

# The framework's own word length (sqlite_search.py:58): below it a term gets no prefix wildcard at all.
_MIN = MIN_WORD_LENGTH

# The default page the spotlight asks for; a caller may ask for less, never for more than the index returns.
_LIMIT = 20

# The dormant toggle for the query split — off, the vocabulary is never consulted and the response is today's.
SPLIT_TOGGLE = "Search::Query::vocabulary"


@frappe.whitelist()
def search(query, type=None, limit=_LIMIT, per_type=None):
	# Empty for a short/blank query or while dormant; `type` is an optional doctype facet.
	query = (query or "").strip()
	engine = CRMLeadSearch()
	status = _status(engine, query)
	if status != "ready":
		return {"results": [], "total": 0, "status": status}

	filters = {"doctype": type} if type else None
	text, understood = _split(query) if is_enabled(SPLIT_TOGGLE) else (query, None)
	if understood:
		filters = {**(filters or {}), **{f["column"]: f["value"] for f in understood["filters"]}}

	res = engine.search(text, filters=filters) or {}

	# Order is the engine's, in get_scoring_pipeline; the TYPED query decides whether an ID was punched in.
	results = [_shape(r, query) for r in res.get("results", [])]
	total = res.get("summary", {}).get("total_matches", len(results))
	# The framework truncates to MAX_SEARCH_RESULTS, so a plateaued count is a floor and the UI must say so.
	out = {
		# `limit` arrives off the wire: cint never raises, and the clamp keeps an absurd value in range.
		"results": _shared(results, per_type)[: max(1, min(cint(limit) or _LIMIT, MAX_SEARCH_RESULTS))],
		# What each kind HAS, counted before the share is taken, so a capped run can offer the rest.
		"totals": dict(Counter(r["doctype"] for r in results)),
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
		# ONE filter per column: two values AND to zero rows, so the rest goes back to the text lane.
		if column in filters:
			spare.append(vocabulary.normalise(value))
		else:
			filters[column] = value
	text = " ".join([*reading.leftover, *spare])
	# A filter NARROWS a text search and never replaces one — FTS5 has no match-everything.
	if not filters or not text:
		return query, None
	shown = [{"column": column, "label": labels.get(column, column), "value": value} for column, value in filters.items()]
	return text, {"filters": shown, "text": text}


def _searchable(query):
	"""What the INDEX will actually see: the query minus anything no tokenizer keeps.

	The floor measured the raw string, so punctuation smuggled a short term past it — `crm/` is four characters
	and cleared a minimum of four, then reached the index as the three-letter token `crm` and prefix-matched
	every row carrying a word that starts with it. The rule is about the TERM, so it is measured on the term."""
	return re.sub(r"[^0-9A-Za-z]+", "", query or "")


def _status(engine, query):
	"""Why an empty list is empty: dormant, too little typed, not yet built, or a real no-match.

	This is the ONE place the query floor is decided. The frontend holds no minimum of its own — it renders
	the status it is handed — so the rule cannot drift between the two and silently swallow a valid search."""
	if not engine.is_search_enabled():
		return "disabled"
	if len(_searchable(query)) < _MIN:
		return "too_short"
	if not engine.index_exists() or not engine._is_indexing_complete():
		return "building"
	return "ready"


def _shared(results, per_type):
	"""At most `per_type` rows of each doctype, in the order the engine ranked them.

	One budget is one doctype's: leads outrank files by a weight that dominates every other factor, so a
	mixed answer of twenty is twenty leads and a matching file is unreachable. A caller showing several
	kinds at once asks for a share each; a caller showing one kind passes nothing and is untouched."""
	cap = cint(per_type)
	if not cap:
		return results
	taken, out = Counter(), []
	for row in results:
		dt = row.get("doctype")
		if taken[dt] >= cap:
			continue
		taken[dt] += 1
		out.append(row)
	return out


def _shape(r, query):
	# The row's fixed slots plus what it needs to open; a unique ID reaches the response only via `ident`.
	dt = r.get("doctype")
	hit = {
		"doctype": dt,
		"name": r.get("name"),
		"lead": r.get("lead") or (r.get("name") if dt == "CRM Lead" else None),
		"tab": TAB.get(dt),
		"title": r.get("title"),
		# The patient a row SHOWS. A file is titled by its own name, so this is how its row says whose it is.
		"lead_name": r.get("lead_name"),
		"snippet": r.get("content"),
		"phone": r.get("phone"),
		# Resolved at index time through `taxonomy.labels.stage_of`.
		"stage": r.get("stage"),
		"stage_color": r.get("stage_color"),
		"vertical": r.get("vertical"),
		"group": r.get("lead_group"),
		"program": r.get("program"),
		# The lead's OWNER; the index column keeps the old name because renaming it rebuilds 173k rows.
		"lead_owner": r.get("assignee"),
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
	from tatva_connect.search.activation import rebuild

	engine = CRMLeadSearch()
	# Dormant, the build returns before it does anything (sqlite_search.py:1765), so a drop would delete an index nothing then replaces.
	if not engine.is_search_enabled():
		return {"queued": False}
	rebuild(engine)
	return {"queued": True}
