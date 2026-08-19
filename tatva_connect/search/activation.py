# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Activator for the Search::Index::indexing toggle — enqueues Frappe's own long-queue build; no custom indexing."""
import frappe

from tatva_connect.search.index import CRMLeadSearch

_BUILD = "frappe.search.sqlite_search.build_index"
_CLASS = "tatva_connect.search.index.CRMLeadSearch"


def apply(enabled):
	# On enable, build once if missing; an index already there is left exactly as it is.
	if not enabled:
		return
	engine = CRMLeadSearch()
	if engine.index_exists():
		return
	rebuild(engine)


def reconcile_index_schema():
	# _ensure_fts_table is CREATE VIRTUAL TABLE IF NOT EXISTS, so an INDEX_SCHEMA edit reaches NO site that already
	# has an index — the columns stay as built and the migrate goes green. An index whose stamp is stale is dropped
	# here and rebuilt; dormant, absent and mid-build all leave the file alone (a cut-short build is never dropped).
	engine = CRMLeadSearch()
	if not engine.is_search_enabled() or not engine.index_exists():
		return
	if not engine._is_indexing_complete():
		return
	if engine.stored_fingerprint() == engine.schema_fingerprint():
		return
	rebuild(engine)


def rebuild(engine):
	"""THE way this app rebuilds: drop, then force. Both steps are load-bearing and neither works alone.

	DROP, because `sqlite_search.build_index` takes its temp-file path only when the index is ABSENT (:309).
	Left in place it builds into the live file — no atomic swap, an index that reads as incomplete for the
	whole run, and if the worker dies a live index with no temp file, which is the one state neither frappe's
	3-hourly check nor `sweep_index_health` will touch. Search then says `building` for ever.

	FORCE, because that same function only builds when `is_continuation or force` (:1772), so the 3-hourly
	`build_index_if_not_exists` (force=False) would never pick a dropped index back up.
	"""
	engine.drop_index()
	enqueue_build()


def enqueue_build():
	# The native full build, deduplicated by the class path frappe's own `build_index_in_background` uses, so our trigger and frappe's collapse into one job instead of racing.
	frappe.enqueue(
		_BUILD,
		queue="long",
		timeout=2 * 60 * 60 + 600,
		search_class_path=_CLASS,
		force=True,
		deduplicate=True,
		job_id=_CLASS,
	)
