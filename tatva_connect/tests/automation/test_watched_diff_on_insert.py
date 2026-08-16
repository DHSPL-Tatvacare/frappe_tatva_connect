# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`context.diff_watched_fields` on an INSERT — the before-image that made every `changed to` deaf.

frappe runs `on_update` inside `insert()` (`document.py:493`), so the dispatcher already reaches an
Updated-entry workflow on a newly-created record. With no before-image the diff was empty, `_changed_match`
found no `__before` key, and the run died on a clean non-match — which silently deafened all 38 trigger nodes
in the product (`crm_task.status changed to Done`, across Anaya, TatvaPractice and Inside-Sales) to an
activity logged ad hoc, because `save_activity` INSERTS the task already carrying its computed status.

Every behaviour here carries its planted-bad twin, the same way `test_operators` does: the suite must prove
it is not blind. The three that must NOT change are the anti-re-fire guard, the migration re-save, and
`changed from…to`.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import context as ctx_build
from tatva_connect.automation import rules

CHANGED_TO_DONE = {"type": "rule", "field": "crm_task.status", "operator": "changed to", "value": "Done"}
FROM_TODO_TO_DONE = {"type": "rule", "field": "crm_task.status", "operator": "changed from…to",
                     "from_value": "Todo", "value": "Done"}


class TestWatchedDiffOnInsert(FrappeTestCase):
	"""An insert diffs against an empty before; a re-save that merely lost its before-image still does not."""

	def _task(self, status="Done"):
		"""An unsaved CRM Task — the diff reads the in-memory doc, so nothing needs to be written."""
		doc = frappe.new_doc("CRM Task")
		doc.status = status
		return doc

	def _fires(self, doc, condition=CHANGED_TO_DONE):
		changed = ctx_build.diff_watched_fields(doc)
		ctx = ctx_build.context_for(doc, changed, lead=None)
		return rules.predicate_match(condition, ctx, ctx_build.field_types_for("CRM Task", "CRM Lead"))

	# --- the defect this closes -------------------------------------------------------------
	def test_insert_diffs_against_an_empty_before(self):
		doc = self._task("Done")
		doc.flags.in_insert = True
		self.assertEqual(ctx_build.diff_watched_fields(doc).get("status"), (None, "Done"))

	def test_adhoc_punch_fires_changed_to(self):
		doc = self._task("Done")
		doc.flags.in_insert = True
		self.assertTrue(self._fires(doc))

	def test_insert_planted_bad_wrong_status(self):
		"""Born Todo, not Done — the operator must still discriminate on the value."""
		doc = self._task("Todo")
		doc.flags.in_insert = True
		self.assertFalse(self._fires(doc))

	# --- what must NOT change ---------------------------------------------------------------
	def test_re_save_without_before_image_is_still_empty(self):
		"""No before-state and NOT an insert — a migration re-save. Unchanged: a clean non-match."""
		doc = self._task("Done")
		doc.flags.in_insert = False
		self.assertEqual(ctx_build.diff_watched_fields(doc), {})
		self.assertFalse(self._fires(doc))

	def test_changed_from_to_does_not_match_on_insert(self):
		"""The before is None on an insert, never `Todo`, so a pinned from-value cannot match."""
		doc = self._task("Done")
		doc.flags.in_insert = True
		self.assertFalse(self._fires(doc, FROM_TODO_TO_DONE))

	def test_unwatched_doctype_still_returns_empty(self):
		"""The cheap early return survives: a doctype the registry does not watch diffs nothing."""
		lead = frappe.new_doc("CRM Lead")
		lead.flags.in_insert = True
		self.assertEqual(ctx_build.diff_watched_fields(lead), {})

	def test_insert_with_no_status_is_not_a_change(self):
		"""A watched field that arrives empty moved from nothing to nothing — the loop drops it."""
		doc = frappe.new_doc("CRM Task")
		doc.status = None
		doc.flags.in_insert = True
		self.assertNotIn("status", ctx_build.diff_watched_fields(doc))
