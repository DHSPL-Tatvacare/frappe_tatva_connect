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
