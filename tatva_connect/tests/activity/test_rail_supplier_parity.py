# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The rail reads the same whichever supplier serves it — flipping the index toggle changes the mechanics, never the page.

One lead with every kind of history, indexed by `timeline.rebuild`, then both suppliers asked the same question.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.activity.test_rail_supplier_parity
"""
import frappe
from frappe.desk.form import assign_to
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import timeline
from tatva_connect.api import activities, partner
from tatva_connect.tests.activity.test_timeline_index import _file_on


def _seen(row):
	"""What the rail draws from a row — the fields `railEvent` and the cards read, never which supplier made it."""
	return (row.get("activity_type") or row.get("kind"), str(row.get("name")), str(row.get("creation")),
			row.get("owner"), row.get("status"), tuple(tuple(c.items()) for c in row.get("changes") or ()))


class TestBothSuppliersDrawTheSameRail(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": "ZZ Parity", "mobile_no": "+919000000075"}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
		self._save(lambda d: setattr(d, "website", "https://zz-parity.example"))
		self._save(lambda d: partner._apply_children(
			d, {"custom_screening_answers": [{"question": "zz_parity_q", "label": "High BP?", "value": "No"}]}))
		self._save(lambda d: d.append("custom_lab_profile", {"hba1c": 7.1, "report_date": "2026-09-01"}))
		note = frappe.get_doc({"doctype": "FCRM Note", "title": "ZZ parity note", "content": "x",
							   "reference_doctype": "CRM Lead", "reference_docname": self.lead.name}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self.files = []
		_file_on("CRM Lead", self.lead.name, "parity_lead", self.files)
		_file_on("FCRM Note", note.name, "parity_note", self.files)
		task = frappe.get_doc({"doctype": "CRM Task", "title": "ZZ parity task", "reference_doctype": "CRM Lead",
							   "reference_docname": self.lead.name}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		frappe.get_doc({"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "CRM Lead",
						"reference_name": self.lead.name, "content": "ZZ parity comment"}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		assign_to.add({"assign_to": ["Administrator"], "doctype": "CRM Lead", "name": self.lead.name})
		task.reload()
		task.status = "Done"
		task.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self._save(lambda d: setattr(d, "website", None))
		timeline.rebuild(self.lead.name)

	def tearDown(self):
		# Files first, before the rollback: a File's bytes are not transactional (see test_timeline_index._drop_files).
		for name in self.files:
			if frappe.db.exists("File", name):
				frappe.delete_doc("File", name, force=True, ignore_permissions=True)  # authz-ok: tier-a — test fixture cleanup
		frappe.db.rollback()

	def _save(self, mutate):
		doc = frappe.get_doc("CRM Lead", self.lead.name)
		mutate(doc)
		# frappe skips the Version in a test unless asked (document.py:556) — and the Version is what the rail reads.
		doc.save(ignore_permissions=True, ignore_version=False)  # authz-ok: tier-a — test fixture

	def test_the_same_page_and_the_same_count(self):
		merged, merged_total = activities._rail_from_merge(self.lead.name, 50, "creation desc")
		indexed, indexed_total = activities._rail_from_index(self.lead.name, 50, "creation desc")

		self.assertEqual([_seen(r) for r in indexed], [_seen(r) for r in merged])
		self.assertEqual(indexed_total, merged_total)

	def test_the_same_page_under_every_filter(self):
		"""Type, Date and both together narrow each supplier to the same page and the same count."""
		today = frappe.utils.today()
		for picked in ({"kind": "Task"}, {"kind": ["in", ["Field Change", "Assignment"]]}, {"kind": ["!=", "Comment"]},
					   {"kind": "File"}, {"creation": today}, {"creation": ["timespan", "last 7 days"]},
					   {"kind": "Note", "creation": [">", today]}, {"creation": ["between", None]},
					   {"creation": ["between", [today, None]]}, {"creation": ["timespan", None]}):
			narrowing = activities._rail_narrowing(picked)
			merged, merged_total = activities._rail_from_merge(self.lead.name, 50, "creation desc", narrowing=narrowing)
			indexed, indexed_total = activities._rail_from_index(self.lead.name, 50, "creation desc", narrowing=narrowing)
			with self.subTest(picked=picked):
				self.assertEqual([_seen(r) for r in indexed], [_seen(r) for r in merged])
				self.assertEqual(indexed_total, merged_total)

	def test_a_file_is_a_line_only_when_filed_on_the_lead(self):
		rows, _total = activities._rail_from_merge(self.lead.name, 50, "creation desc")
		self.assertEqual([r["file_name"] for r in rows if r["kind"] == "file"],
						 [frappe.db.get_value("File", self.files[0], "file_name")])

	def test_a_type_keeps_only_its_own_lines(self):
		rows, _total = activities._rail_from_merge(self.lead.name, 50, "creation desc",
												   narrowing=activities._rail_narrowing({"kind": "Task"}))
		self.assertEqual({r.get("activity_type") or r["kind"] for r in rows}, {"task", "task_closed"})

	def test_an_unsupported_date_operator_says_so(self):
		with self.assertRaises(frappe.ValidationError):
			activities._rail_narrowing({"creation": ["!=", frappe.utils.today()]})
