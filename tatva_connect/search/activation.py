# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Activator for the Search::Index::indexing toggle — enqueues Frappe's own long-queue build; no custom indexing."""
import frappe

from tatva_connect.search.index import CRMLeadSearch

_BUILD = "frappe.search.sqlite_search.build_index"
_CLASS = "tatva_connect.search.index.CRMLeadSearch"


def apply(enabled):
	# On enable, build once if missing; already-built is a no-op and the 3-hourly cron resumes a cut-short build.
	if not enabled:
		return
	if CRMLeadSearch().index_exists():
		return
	enqueue_build()


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
	engine.drop_index()
	# force=True is load-bearing: sqlite_search.build_index only builds when `is_continuation or force` (16.22.0
	# line 1772), so the 3-hourly build_index_if_not_exists (force=False) never rebuilds a dropped index at all.
	enqueue_build(force=True)


def enqueue_build(force=False):
	# The native full build, deduplicated by class path so a double-trigger cannot stack two.
	frappe.enqueue(
		_BUILD,
		queue="long",
		timeout=2 * 60 * 60 + 600,
		search_class_path=_CLASS,
		force=force,
		deduplicate=True,
		job_id=_CLASS,
	)
