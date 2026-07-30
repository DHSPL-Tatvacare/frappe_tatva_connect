# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Activity rail's index is a faithful, disposable projection of the source tables.

Step 2 of docs/plans/2026-07-27-lead-detail-server-paging-leads-pattern.md. The rail used to be assembled
by merging six tables on every open — 264 KB and 459 rows to paint 20, on the fattest dev lead — and every
new rail type would have added another leg. The merge moved to write time: one pointer row per event,
`(reference_doctype, reference_name, event_on)` indexed, so a page is one seek at any lead size.

Because it is derived, the only things that can go wrong are FAITHFULNESS and DRIFT, and this module locks
both:

  * **A lead's documents are not the ones filed against the lead.** Frappe gives a File one parent, and
    each surface parents its own — a note's file belongs to the FCRM Note, an emailed one to the
    Communication. The Attachments tab shows all of them deliberately, so a rep never has to remember
    where a document was added. An index that asked the File table for `attached_to_name = <lead>` would
    hold a fraction of them and the rail would quietly lose the rest. `test_a_file_on_a_note_*` and
    `test_a_file_on_a_task_*` are that lock, and they fail on an index that resolves only direct parents.
  * **Not everything with a parent is a rail event.** A user's avatar is a File too.
  * **Dormant by default.** The index is an operator toggle and ships OFF; while it is off nothing is
    written and the rail serves from the old read-time merge. A hook that wrote regardless would fill a
    table on every site whether or not the operator ever enabled it.
  * **Repairable.** `rebuild()` regenerates a lead from source and `reconcile()` reports source-vs-index
    per kind. Both must agree, and a second rebuild must not duplicate — that idempotency is what makes
    the table safe to throw away and rebuild rather than a thing to be careful with.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.activity.test_timeline_index
"""
import os

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import timeline


# A FILE'S NAME IS NOT PREDICTABLE, and this module used to assume it was. Core appends a six-character
# suffix when a file of that name is already on disk (`core/doctype/file/utils.py:192`), and a test's DB rows
# roll back while its FILES do not — so the first run passed and every run after was handed
# `zz_on_lead27dd56.txt` and failed on the name it had hardcoded. 28 files had accumulated on the bench.
# Two fixes, both already used by `tests/storage/test_file_lifecycle_seam.py`, which is the suite that owns
# this ground: assert the name the file ACTUALLY got, and delete every File this module makes.
def _file_on(parent_doctype, parent_name, label, tracker):
	"""One private File on a parent, its bytes unique so core cannot dedup it onto an earlier one."""
	doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"zz_{label}_{frappe.generate_hash(length=8)}.txt",
			"content": frappe.generate_hash(length=16),
			"is_private": 1,
			"attached_to_doctype": parent_doctype,
			"attached_to_name": parent_name,
		}
	).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
	tracker.append(doc.name)
	return doc


class TestTimelineIndex(FrappeTestCase):
	def setUp(self):
		self.files = []
		self.lead = frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": "ZZ Timeline Probe", "mobile_no": "+919000000044"}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
		self.note = frappe.get_doc(
			{
				"doctype": "FCRM Note",
				"title": "ZZ timeline note",
				"content": "x",
				"reference_doctype": "CRM Lead",
				"reference_docname": self.lead.name,
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
		self.task = frappe.get_doc(
			{
				"doctype": "CRM Task",
				"title": "ZZ timeline task",
				"reference_doctype": "CRM Lead",
				"reference_docname": self.lead.name,
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator

	def tearDown(self):
		# FILES FIRST, BEFORE THE ROLLBACK, and this order is the whole fix. A File's row is transactional
		# and its BYTES are not, so the rollback below erases the row and abandons the file on disk — which is
		# what let 28 of them pile up and break every run after the first. `delete_doc` is what reaches
		# `File.on_trash` and reclaims the bytes, and it can only find a row that still exists.
		# unittest runs tearDown even when a test raises, so this needs no addCleanup — and an addCleanup
		# would be WRONG here: cleanups run AFTER tearDown, by which point the rollback has removed the rows.
		self._drop_files()
		frappe.db.delete("CRM Timeline Event", {"reference_name": self.lead.name})
		frappe.db.rollback()

	def _drop_files(self):
		"""Delete the row AND the local copy, because in a test only the row is anyone else's job.

		The storage layer already removes the local bytes after an offload — but through
		`enqueue_after_commit` (`storage/file_events.py:92`), deliberately, so bytes are never dropped on an
		uncommitted or failed upload. A test's transaction ROLLS BACK, so that job never runs and the staging
		copy stays. That is the layer behaving correctly; it just means a fixture owns its own bytes.
		"""
		from frappe.utils import get_files_path

		for name in self.files:
			if not frappe.db.exists("File", name):
				continue
			doc = frappe.get_doc("File", name)
			local = get_files_path(doc.file_name, is_private=bool(doc.is_private))
			frappe.delete_doc("File", name, force=True, ignore_permissions=True)  # authz-ok: tier-a — test fixture cleanup
			if os.path.exists(local):
				os.remove(local)

	def _indexed_files(self):
		names = frappe.get_all(
			"CRM Timeline Event",
			filters={"reference_name": self.lead.name, "source_doctype": "File"},
			pluck="source_name",
		)
		return set(frappe.get_all("File", filters={"name": ["in", names]}, pluck="file_name"))

	def test_a_file_on_the_lead_is_indexed(self):
		doc = _file_on("CRM Lead", self.lead.name, "on_lead", self.files)
		timeline.rebuild(self.lead.name)
		self.assertIn(doc.file_name, self._indexed_files())

	def test_a_file_on_a_note_reaches_the_leads_rail(self):
		"""The aggregation is deliberate: a rep should not have to remember that this document was
		uploaded on the note rather than on the lead. An index resolving only direct parents loses it."""
		doc = _file_on("FCRM Note", self.note.name, "on_note", self.files)
		timeline.rebuild(self.lead.name)
		self.assertIn(doc.file_name, self._indexed_files())

	def test_a_file_on_a_task_reaches_the_leads_rail(self):
		doc = _file_on("CRM Task", self.task.name, "on_task", self.files)
		timeline.rebuild(self.lead.name)
		self.assertIn(doc.file_name, self._indexed_files())

	def test_a_file_that_belongs_to_no_rail_is_ignored(self):
		"""An avatar is a File with a parent. It is not a thing that happened on a patient."""
		avatar = _file_on("User", "Administrator", "avatar", self.files)
		self.assertIsNone(timeline.event_row(avatar))

	def test_reconcile_agrees_with_source_after_a_rebuild(self):
		_file_on("FCRM Note", self.note.name, "recon", self.files)
		timeline.rebuild(self.lead.name)
		for kind, counts in timeline.reconcile(self.lead.name).items():
			self.assertEqual(counts["source"], counts["index"], f"{kind} drifted")

	def test_rebuild_is_idempotent(self):
		"""The table is safe to throw away and regenerate — which is only true if regenerating twice
		does not double it."""
		_file_on("FCRM Note", self.note.name, "twice", self.files)
		first = timeline.rebuild(self.lead.name)
		rows_after_first = frappe.db.count("CRM Timeline Event", {"reference_name": self.lead.name})
		second = timeline.rebuild(self.lead.name)
		self.assertEqual(first, second)
		self.assertEqual(
			rows_after_first,
			frappe.db.count("CRM Timeline Event", {"reference_name": self.lead.name}),
		)

	def test_a_deleted_source_takes_its_pointer_with_it(self):
		"""A rail must not cite a ghost."""
		doc = _file_on("CRM Lead", self.lead.name, "doomed", self.files)
		timeline.rebuild(self.lead.name)
		self.assertIn(doc.file_name, self._indexed_files())
		timeline.drop_event(doc)
		self.assertNotIn(doc.file_name, self._indexed_files())

	def test_nothing_is_written_while_the_toggle_is_dormant(self):
		"""Ships OFF, like every automation here. The hook must be inert until an operator enables it."""
		self.assertFalse(
			frappe.db.get_value("CRM Tatva Automation", timeline.TOGGLE, "enabled"),
			"the timeline index must ship dormant",
		)
		before = frappe.db.count("CRM Timeline Event", {"reference_name": self.lead.name})
		timeline.index_event(_file_on("CRM Lead", self.lead.name, "dormant", self.files))
		self.assertEqual(
			before, frappe.db.count("CRM Timeline Event", {"reference_name": self.lead.name})
		)
