# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A dormant feature writes nothing. Not even a row saying it did nothing.

`Location::Google::capture` gates the visit trail: GPS capture, the anchor/radius guard, and the
CRM Visit Audit row that records the verdict. With the switch off — or with the grain simply not in
`location_tracked_grains` — none of that applies.

It nevertheless wrote an audit row on EVERY activity, saying "Not Required", on the rep's path and
the partner API's alike. 25,794 of them on the dev site, every single one a "Not Required": the table
had never held a real audit. And because an audit row must name its task, the writer inserted an
empty task SHELL first and saved the real values over it — so a switched-off feature was paying for
two of the three writes every activity made.

These tests pin the rule the platform is built on: switch off means no-op. Nothing is written
anywhere.
"""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.activity.api import save_activity

VERTICAL, GROUP = "GoodFlip Care", "Anaya"


class TestDormantLocationWritesNothing(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		# A type scoped to a PROGRAM only applies to a lead already on that program. The probe lead
		# carries none, so the type it is completed with must carry none either — picking any type on
		# the grain works only for as long as the first one happens to be unscoped.
		cls.task_type = frappe.db.get_value(
			"CRM Task Type",
			{"name": ["like", f"{VERTICAL}::{GROUP}::%"], "program": ["in", ["", None]]},
			"name",
		)
		if not cls.task_type:
			raise unittest.SkipTest(f"no program-agnostic task type on {VERTICAL}::{GROUP}")

	def setUp(self):
		self.sp = f"dormant_loc_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Dormant Probe",
			"mobile_no": f"+9198123{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": VERTICAL, "custom_group": GROUP,
		}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.db.rollback(save_point=self.sp)

	def _audits(self):
		return frappe.db.count("CRM Visit Audit", {"lead": self.lead.name})

	def test_an_untracked_grain_writes_no_visit_audit(self):
		"""The grain is not location-tracked, so there is no visit trail to keep."""
		with patch("tatva_connect.location.api.is_location_tracked", return_value=None):
			save_activity(self.lead.name, self.task_type, {}, task=None)
		self.assertEqual(self._audits(), 0,
		                 "a dormant location feature must not write an audit row")

	def test_an_untracked_grain_inserts_the_task_ONCE(self):
		"""No audit means nothing needs the task's id before it exists, so the shell insert — and the
		second write it forced — is not needed. This is the other half of the cost."""
		inserts = []
		real_insert = frappe.model.document.Document.insert

		def spy(doc, *a, **kw):
			if doc.doctype == "CRM Task":
				inserts.append(doc.name)
			return real_insert(doc, *a, **kw)

		with patch("tatva_connect.location.api.is_location_tracked", return_value=None), \
		     patch.object(frappe.model.document.Document, "insert", spy):
			name = save_activity(self.lead.name, self.task_type, {}, task=None)

		self.assertEqual(len(inserts), 1,
		                 "an untracked grain must insert the CRM Task once, not a shell then a save")
		self.assertTrue(frappe.db.exists("CRM Task", name))

	def test_the_task_still_carries_its_computed_fields(self):
		"""Cutting the second write must not cut the values it used to carry."""
		with patch("tatva_connect.location.api.is_location_tracked", return_value=None):
			name = save_activity(self.lead.name, self.task_type, {}, task=None)
		row = frappe.db.get_value("CRM Task", name,
		                          ["custom_task_type", "status", "custom_activity_payload",
		                           "reference_doctype", "reference_docname"], as_dict=True)
		self.assertEqual(row.custom_task_type, self.task_type)
		self.assertEqual(row.reference_docname, self.lead.name, "the task must still bind to its lead")
		self.assertIsNotNone(row.custom_activity_payload, "compute still ran")
		self.assertIn(row.status, ("Todo", "Done"))

	def test_a_TRACKED_grain_still_writes_its_audit(self):
		"""The other direction. Switch the feature ON and the trail comes back, shell and all — the
		fix must not have quietly disabled the feature itself."""
		with patch("tatva_connect.location.api.is_location_tracked", return_value=200), \
		     patch("tatva_connect.location.api.location_required", return_value=None):
			save_activity(self.lead.name, self.task_type, {}, task=None)
		self.assertEqual(self._audits(), 1,
		                 "with tracking LIVE, a phone activity still records Not Required — that is a "
		                 "real entry in the trail, not noise")
