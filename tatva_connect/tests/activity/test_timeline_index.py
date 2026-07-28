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
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import timeline


def _file_on(parent_doctype, parent_name, file_name):
	return frappe.get_doc(
		{
			"doctype": "File",
			"file_name": file_name,
			"content": "zz",
			"is_private": 1,
			"attached_to_doctype": parent_doctype,
			"attached_to_name": parent_name,
		}
	).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator


class TestTimelineIndex(FrappeTestCase):
	def setUp(self):
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
		frappe.db.delete("CRM Timeline Event", {"reference_name": self.lead.name})
		frappe.db.rollback()

	def _indexed_files(self):
		names = frappe.get_all(
			"CRM Timeline Event",
			filters={"reference_name": self.lead.name, "source_doctype": "File"},
			pluck="source_name",
		)
		return set(frappe.get_all("File", filters={"name": ["in", names]}, pluck="file_name"))

	def test_a_file_on_the_lead_is_indexed(self):
		_file_on("CRM Lead", self.lead.name, "zz_on_lead.txt")
		timeline.rebuild(self.lead.name)
		self.assertIn("zz_on_lead.txt", self._indexed_files())

	def test_a_file_on_a_note_reaches_the_leads_rail(self):
		"""The aggregation is deliberate: a rep should not have to remember that this document was
		uploaded on the note rather than on the lead. An index resolving only direct parents loses it."""
		_file_on("FCRM Note", self.note.name, "zz_on_note.txt")
		timeline.rebuild(self.lead.name)
		self.assertIn("zz_on_note.txt", self._indexed_files())

	def test_a_file_on_a_task_reaches_the_leads_rail(self):
		_file_on("CRM Task", self.task.name, "zz_on_task.txt")
		timeline.rebuild(self.lead.name)
		self.assertIn("zz_on_task.txt", self._indexed_files())

	def test_a_file_that_belongs_to_no_rail_is_ignored(self):
		"""An avatar is a File with a parent. It is not a thing that happened on a patient."""
		avatar = _file_on("User", "Administrator", "zz_avatar.txt")
		self.assertIsNone(timeline.event_row(avatar))

	def test_reconcile_agrees_with_source_after_a_rebuild(self):
		_file_on("FCRM Note", self.note.name, "zz_recon.txt")
		timeline.rebuild(self.lead.name)
		for kind, counts in timeline.reconcile(self.lead.name).items():
			self.assertEqual(counts["source"], counts["index"], f"{kind} drifted")

	def test_rebuild_is_idempotent(self):
		"""The table is safe to throw away and regenerate — which is only true if regenerating twice
		does not double it."""
		_file_on("FCRM Note", self.note.name, "zz_twice.txt")
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
		doc = _file_on("CRM Lead", self.lead.name, "zz_doomed.txt")
		timeline.rebuild(self.lead.name)
		self.assertIn("zz_doomed.txt", self._indexed_files())
		timeline.drop_event(doc)
		self.assertNotIn("zz_doomed.txt", self._indexed_files())

	def test_nothing_is_written_while_the_toggle_is_dormant(self):
		"""Ships OFF, like every automation here. The hook must be inert until an operator enables it."""
		self.assertFalse(
			frappe.db.get_value("CRM Tatva Automation", timeline.TOGGLE, "enabled"),
			"the timeline index must ship dormant",
		)
		before = frappe.db.count("CRM Timeline Event", {"reference_name": self.lead.name})
		timeline.index_event(_file_on("CRM Lead", self.lead.name, "zz_dormant.txt"))
		self.assertEqual(
			before, frappe.db.count("CRM Timeline Event", {"reference_name": self.lead.name})
		)
