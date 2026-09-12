# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Reassign is Clear Assignment then Assign, per row, inside ONE transaction per row.

THE ORDER AND THE GRAIN ARE THE WHOLE POINT. Clearing every row first and assigning afterwards would,
on any failure between the two passes, leave a batch of leads owned by NOBODY — strictly worse than
where they started. `_per_row` commits once per row, so a row that fails rolls back to the assignee it
already had and a half-reassigned lead never exists. That is the claim this file exists to keep true.

It also locks the trap that would otherwise appear only at scale: the action name has to be legal at
BOTH doors. The executor registry is one; the `CRM List Action Job` Select is the other, and the queued
path is the only one that touches it — so a missing option passes every inline test and fails at 20 rows.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.bulk_actions.test_reassign_stacks_clear_and_assign
"""
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import bulk_actions, bulk_actions_run

OLD = "zz-reassign-old@example.com"
NEW = "zz-reassign-new@example.com"
PHONE_PREFIX = "+916100081"


class TestReassignStacksClearAndAssign(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		for email, name in ((OLD, "Reassign Old"), (NEW, "Reassign New")):
			if not frappe.db.exists("User", email):
				frappe.get_doc({"doctype": "User", "email": email, "first_name": name,
				                "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}
				               ).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for email in (OLD, NEW):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"{PHONE_PREFIX}%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def _lead(self, suffix):
		lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": f"Reassign {suffix}",
			"mobile_no": f"{PHONE_PREFIX}{suffix:02d}", "status": "New",
		}).insert(ignore_permissions=True).name
		bulk_actions_run._assign_row("CRM Lead", lead, [OLD])
		frappe.db.commit()
		return lead

	def _owners(self, lead):
		"""Who the row is assigned to right now, as the app itself reads it."""
		return {
			t.allocated_to for t in frappe.get_all(
				"ToDo",
				filters={"reference_type": "CRM Lead", "reference_name": lead, "status": "Open"},
				fields=["allocated_to"],
			)
		}

	def test_the_row_ends_with_only_the_new_assignee(self):
		lead = self._lead(1)
		self.assertEqual(self._owners(lead), {OLD})
		bulk_actions_run.run_reassign("CRM Lead", [lead], {"assign_to": [NEW]})
		self.assertEqual(self._owners(lead), {NEW})

	def test_the_clear_runs_before_the_assign(self):
		"""Order, asserted directly: whatever Reassign calls, it clears that row before it assigns it."""
		lead = self._lead(2)
		calls = []
		with patch.object(bulk_actions_run, "_clear_row", side_effect=lambda *a: calls.append("clear")), \
		     patch.object(bulk_actions_run, "_assign_row", side_effect=lambda *a: calls.append("assign")):
			bulk_actions_run.run_reassign("CRM Lead", [lead], {"assign_to": [NEW]})
		self.assertEqual(calls, ["clear", "assign"])

	def test_a_failing_row_keeps_the_assignee_it_already_had(self):
		"""THE safety claim. The assign half fails; the row must not be left owned by nobody."""
		lead = self._lead(3)
		with patch.object(bulk_actions_run, "_assign_row", side_effect=RuntimeError("boom")):
			result = bulk_actions_run.run_reassign("CRM Lead", [lead], {"assign_to": [NEW]})
		self.assertEqual(result["failed"], 1)
		self.assertEqual(result["succeeded"], 0)
		self.assertEqual(self._owners(lead), {OLD})

	def test_one_bad_row_does_not_take_the_good_one_with_it(self):
		good, bad = self._lead(4), self._lead(5)
		real = bulk_actions_run._assign_row

		def _flaky(doctype, name, assignees):
			if name == bad:
				raise RuntimeError("boom")
			return real(doctype, name, assignees)

		with patch.object(bulk_actions_run, "_assign_row", side_effect=_flaky):
			result = bulk_actions_run.run_reassign("CRM Lead", [good, bad], {"assign_to": [NEW]})
		self.assertEqual((result["succeeded"], result["failed"]), (1, 1))
		self.assertEqual(result["failed_names"], [bad])
		self.assertEqual(self._owners(good), {NEW})
		self.assertEqual(self._owners(bad), {OLD})

	def test_the_action_is_legal_at_the_executor_door(self):
		self.assertIn("Reassign", bulk_actions._EXECUTORS)

	def test_the_action_is_legal_at_the_job_door(self):
		"""The queued path inserts this string; a Select that does not offer it fails only at 20+ rows."""
		options = frappe.get_meta("CRM List Action Job").get_field("action").options.split("\n")
		self.assertIn("Reassign", options)

	def test_the_queued_path_actually_accepts_a_reassign_job(self):
		"""Both doors, proven together — the insert the inline path never performs."""
		docnames = json.dumps([f"CRM-LEAD-{i:04d}" for i in range(bulk_actions.THRESHOLD + 5)])
		with patch.object(bulk_actions.automation, "is_enabled", return_value=True):
			out = bulk_actions.run_or_queue("Reassign", "CRM Lead", docnames, json.dumps({"assign_to": [NEW]}))
		self.assertTrue(out["queued"])
		self.assertEqual(frappe.db.get_value("CRM List Action Job", out["job"], "action"), "Reassign")
		frappe.db.rollback()
