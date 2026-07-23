# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Toggle activator for `Search::Index::indexing`. Called with the enabled state at deploy
(reconcile_activations) and on every runtime flip (CRM Tatva Automation.on_update), plus the manual
Rebuild button. All it does is enqueue Frappe's OWN build on the long queue — no custom indexing."""
import frappe

from tatva_connect.search.index import CRMLeadSearch

_BUILD = "frappe.search.sqlite_search.build_index"
_CLASS = "tatva_connect.search.index.CRMLeadSearch"


def apply(enabled):
	"""On enable, build the index once if it is missing; steady-state (already built) is a no-op, and
	the framework's 3-hourly cron resumes any build the queue cut short. Disable is inert — the flag
	read by is_search_enabled already stops indexing and blanks results."""
	if not enabled:
		return
	if CRMLeadSearch().index_exists():
		return
	enqueue_build()


def enqueue_build(force=False):
	"""Enqueue the native full build, deduplicated by class path so a double-trigger cannot stack two."""
	frappe.enqueue(
		_BUILD,
		queue="long",
		timeout=2 * 60 * 60 + 600,
		search_class_path=_CLASS,
		force=force,
		deduplicate=True,
		job_id=_CLASS,
	)
