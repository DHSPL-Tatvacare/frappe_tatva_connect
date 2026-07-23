# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The one switch for global search. Flipping `enabled` ON, or ticking `rebuild`, enqueues the native
full build on the long queue — the same `frappe.search.sqlite_search.build_index` the framework's own
after_migrate and 3-hourly self-heal use. Nothing here reimplements indexing."""
import frappe
from frappe.model.document import Document

_BUILD = "frappe.search.sqlite_search.build_index"
_CLASS = "tatva_connect.search.index.CRMLeadSearch"


class CRMSearchSettings(Document):
	def on_update(self):
		before = self.get_doc_before_save()
		was_enabled = bool(before and before.enabled)
		if self.enabled and not was_enabled:
			_enqueue_build()
		if self.rebuild:
			_enqueue_build()
			# One-shot: clear the tick (db_set does not re-run on_update).
			self.db_set("rebuild", 0, update_modified=False)


def _enqueue_build():
	"""Deduplicated so a double-save cannot stack two builds; resumed by the framework's 3-hourly cron
	if the queue times out mid-build."""
	frappe.enqueue(
		_BUILD,
		queue="long",
		timeout=2 * 60 * 60 + 600,
		search_class_path=_CLASS,
		force=True,
		deduplicate=True,
		job_id=_CLASS,
	)
