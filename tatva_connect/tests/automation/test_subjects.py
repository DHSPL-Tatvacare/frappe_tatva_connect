# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The SUBJECTS spine (automation.subjects) — the ONE brain for 'which doctypes the engine anchors to,
and how each resolves to its CRM Lead'. Real docs, real resolution, fail-closed for the unresolvable."""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import subjects
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_GRAIN = GRAINS[0]


class TestSubjects(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "SubjProbe", "lead_name": "Subj Probe", "status": "New",
			"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
			"custom_current_program": _GRAIN["program"],
		}).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Task", {"reference_docname": cls.lead.name})
		frappe.db.delete("CRM Lead", {"lead_name": "Subj Probe"})

	# (a) a Lead resolves to itself.
	def test_lead_resolves_to_self(self):
		self.assertEqual(subjects.resolve_lead_name(self.lead), self.lead.name)

	# (b) a Task with reference_doctype=CRM Lead resolves to its parent lead.
	def test_task_resolves_to_parent_lead(self):
		task = frappe.get_doc({"doctype": "CRM Task", "title": "subj task",
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name, "status": "Todo"}).insert(ignore_permissions=True)
		self.assertEqual(subjects.resolve_lead_name(task), self.lead.name)

	# (c) a Task whose reference is NOT a CRM Lead resolves to None (the dynamic-link guard, fail-closed).
	def test_task_non_lead_reference_resolves_none(self):
		# Build in memory (not inserted) — resolution reads reference_doctype off the doc.
		task = frappe.get_doc({"doctype": "CRM Task", "title": "subj task 2",
			"reference_doctype": "CRM Deal", "reference_docname": "nope", "status": "Todo"})
		self.assertIsNone(subjects.resolve_lead_name(task))

	# (c2) an email resolves to the patient it is filed against — the inbound-email trigger's whole seam.
	def test_communication_resolves_to_the_lead_it_references(self):
		comm = frappe.get_doc({
			"doctype": "Communication", "communication_type": "Communication",
			"sent_or_received": "Received", "subject": "Re: your visit", "content": "thanks",
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
			"reference_name": self.lead.name,
		})
		self.assertEqual(subjects.resolve_lead_name(comm), self.lead.name)

	# (c3) a cold email references nothing, so no workflow can act on it.
	def test_communication_with_no_lead_reference_resolves_none(self):
		comm = frappe.get_doc({
			"doctype": "Communication", "communication_type": "Communication",
			"sent_or_received": "Received", "subject": "hello", "content": "hi",
		})
		self.assertIsNone(subjects.resolve_lead_name(comm))

	# (c4) a file the patient emailed in is the patient's, through the email — `file_access.root_of`.
	def test_file_on_a_communication_resolves_through_it_to_the_lead(self):
		comm = frappe.get_doc({
			"doctype": "Communication", "communication_type": "Communication",
			"sent_or_received": "Received", "subject": "Probe: report", "content": "attached",
			"reference_doctype": "CRM Lead", "reference_name": self.lead.name,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		self.addCleanup(frappe.delete_doc, "Communication", comm.name, force=True, ignore_permissions=True)
		f = frappe.get_doc({"doctype": "File", "file_name": "report.txt", "content": "x",
		                    "attached_to_doctype": "Communication", "attached_to_name": comm.name})
		self.assertEqual(subjects.resolve_lead_name(f), self.lead.name)

	# (c5) the second hop is guarded too: a surface that is not a patient's still resolves to nothing.
	def test_file_on_a_communication_about_nobody_resolves_none(self):
		comm = frappe.get_doc({
			"doctype": "Communication", "communication_type": "Communication",
			"sent_or_received": "Received", "subject": "Probe: stray", "content": "hi",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		self.addCleanup(frappe.delete_doc, "Communication", comm.name, force=True, ignore_permissions=True)
		f = frappe.get_doc({"doctype": "File", "file_name": "stray.txt", "content": "x",
		                    "attached_to_doctype": "Communication", "attached_to_name": comm.name})
		self.assertIsNone(subjects.resolve_lead_name(f))

	# (c6) a file on a doctype no surface map names is still nothing — the old guard, unchanged.
	def test_file_on_an_unmapped_surface_resolves_none(self):
		f = frappe.get_doc({"doctype": "File", "file_name": "x.txt", "content": "x",
		                    "attached_to_doctype": "CRM Vertical", "attached_to_name": "nope"})
		self.assertIsNone(subjects.resolve_lead_name(f))

	# (d) an unknown doctype resolves to None.
	def test_unknown_doctype_resolves_none(self):
		self.assertIsNone(subjects.resolve_lead_name(frappe._dict({"doctype": "Customer", "name": "X"})))

	# (e) the subject set IS the SUBJECTS map (the watch-doctype gate + drift list derive from it).
	def test_subject_doctypes_match_map(self):
		self.assertEqual(set(subjects.subject_doctypes()), set(subjects.SUBJECTS))
		self.assertTrue(subjects.is_subject("CRM Lead"))
		self.assertFalse(subjects.is_subject("Customer"))


if __name__ == "__main__":
	unittest.main()
