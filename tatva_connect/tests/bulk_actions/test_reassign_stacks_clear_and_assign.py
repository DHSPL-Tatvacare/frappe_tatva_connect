# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Reassign is Clear then Assign per row, one transaction per row, so a failing row keeps the assignee it had; its action name is legal at both doors (the executor registry and the CRM List Action Job Select)."""
import json
from unittest.mock import patch

import frappe
from frappe import share
from frappe.tests.utils import FrappeTestCase

from tatva_connect import bulk_actions, bulk_actions_run
from tatva_connect.notifications import batch, dispatch

OLD = "zz-reassign-old@example.com"
NEW = "zz-reassign-new@example.com"
CROWD = [f"zz-reassign-crowd{i}@example.com" for i in range(4)]
MANAGER = "zz-reassign-manager@example.com"
PHONE_PREFIX = "+916100081"
# The two switches that make owner and assignee move together; armed for this file, restored after.
SWITCHES = ("Lead::Assignment::owner", "Lead::Assignment::from_rule")


class TestReassignStacksClearAndAssign(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		cls._switches = {key: frappe.db.get_value("CRM Tatva Automation", key, "enabled") for key in SWITCHES}
		cls._arm(dict.fromkeys(SWITCHES, 1))
		for email, name in ((OLD, "Reassign Old"), (NEW, "Reassign New"), *((c, "Reassign Crowd") for c in CROWD)):
			if not frappe.db.exists("User", email):
				frappe.get_doc({"doctype": "User", "email": email, "first_name": name,
				                "send_welcome_email": 0, "roles": [{"role": "Sales User"}, {"role": "Wiki User"}]}
				               ).insert(ignore_permissions=True)  # Wiki User up front: wiki adds it after insert unless present
		if not frappe.db.exists("User", MANAGER):
			frappe.get_doc({"doctype": "User", "email": MANAGER, "first_name": "Reassign Manager", "send_welcome_email": 0,
			                "roles": [{"role": "Sales Manager"}, {"role": "Wiki User"}]}).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		cls._arm(cls._switches)
		for email in (OLD, NEW, MANAGER, *CROWD):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		"""Every executor runs inside a list action's batch, as the queued door runs it; what it would tell is recorded, never sent."""
		self.enterContext(batch.holding("CRM List Action Job", "zz-reassign"))
		self.enterContext(patch.object(dispatch, "armed", return_value=True))
		self.counted = self.enterContext(patch.object(batch, "count"))

	@staticmethod
	def _arm(states):
		for key, enabled in states.items():
			row = frappe.get_doc("CRM Tatva Automation", key)
			row.enabled = enabled
			row.save(ignore_permissions=True)

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"{PHONE_PREFIX}%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def _lead(self, suffix):
		lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": f"Reassign {suffix}",
			"mobile_no": f"{PHONE_PREFIX}{suffix:03d}", "status": "New",
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

	def test_the_clear_reaches_a_sixth_holder(self):
		"""frappe's `assign_to.get` answers five holders; a clear that reads through it leaves the sixth holding the lead."""
		lead = self._lead(6)
		bulk_actions_run._assign_row("CRM Lead", lead, [*CROWD, NEW])
		self.assertEqual(len(self._owners(lead)), 6)
		bulk_actions_run._clear_row("CRM Lead", lead)
		self.assertEqual(self._owners(lead), set())

	def test_the_clear_runs_before_the_assign(self):
		"""Order, asserted directly: whatever Reassign calls, it clears that row before it assigns it."""
		lead = self._lead(2)
		calls = []
		with patch.object(bulk_actions_run, "_clear_row", side_effect=lambda *a, **k: calls.append("clear")), \
		     patch.object(bulk_actions_run, "_assign_row", side_effect=lambda *a, **k: calls.append("assign")):
			bulk_actions_run.run_reassign("CRM Lead", [lead], {"assign_to": [NEW]})
		self.assertEqual(calls, ["clear", "assign"])

	def test_a_holder_whose_only_grant_is_their_own_assignment_can_reassign_it(self):
		"""RED before: the clear cancelled the holder's ToDo, the assign re-asked READ and the row rolled back."""
		lead = self._lead(16)
		holder = CROWD[3]
		bulk_actions_run._assign_row("CRM Lead", lead, [holder])
		frappe.db.commit()
		frappe.set_user(holder)
		try:
			out = bulk_actions_run.run_reassign("CRM Lead", [lead], {"assign_to": [NEW]})
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(out["failed"], 0)
		self.assertEqual(self._owners(lead), {NEW})

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

		def _flaky(doctype, name, assignees, **kwargs):
			if name == bad:
				raise RuntimeError("boom")
			return real(doctype, name, assignees, **kwargs)

		with patch.object(bulk_actions_run, "_assign_row", side_effect=_flaky):
			result = bulk_actions_run.run_reassign("CRM Lead", [good, bad], {"assign_to": [NEW]})
		self.assertEqual((result["succeeded"], result["failed"]), (1, 1))
		self.assertEqual(result["failed_names"], [bad])
		self.assertEqual(self._owners(good), {NEW})
		self.assertEqual(self._owners(bad), {OLD})

	def test_a_clear_takes_every_assignee_and_keeps_the_owner(self):
		"""Clear is frappe's: every assignee off with their share, the owner stays so the lead never drops out of view."""
		lead = self._lead(7)
		bulk_actions_run._assign_row("CRM Lead", lead, [CROWD[0]])
		share.add_docshare("CRM Lead", lead, CROWD[0], flags={"ignore_share_permission": True})
		bulk_actions_run.run_clear_assignment("CRM Lead", [lead], {})
		self.assertEqual(self._owners(lead), set())
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "lead_owner"), OLD)
		self.assertFalse(frappe.db.exists("DocShare", {"share_doctype": "CRM Lead", "share_name": lead, "user": CROWD[0]}))

	def test_a_reassign_leaves_the_new_person_as_owner_and_only_assignee(self):
		lead = self._lead(8)
		frappe.set_user(MANAGER)
		try:
			bulk_actions_run.run_reassign("CRM Lead", [lead], {"assign_to": [NEW]})
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._owners(lead), {NEW})
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "lead_owner"), NEW)

	def _alerts(self, user, lead):
		return frappe.db.count("Notification Log", {"for_user": user, "document_type": "CRM Lead", "document_name": lead})

	def _shared(self, lead, user):
		return frappe.db.exists("DocShare", {"share_doctype": "CRM Lead", "share_name": lead, "user": user})

	def test_a_new_owner_removes_no_one_and_alerts_once_per_batch(self):
		"""Bulk Edit on Lead Owner changes the owner only, silently per lead, and tells the new owner once."""
		lead = self._lead(9)
		bulk_actions_run._assign_row("CRM Lead", lead, [CROWD[0]])
		self.counted.reset_mock()
		bulk_actions_run.run_bulk_edit("CRM Lead", [lead], {"field": "lead_owner", "value": NEW})
		self.assertEqual(self._owners(lead), {OLD, CROWD[0], NEW})
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "lead_owner"), NEW)
		self.assertEqual(self._alerts(NEW, lead), 0)
		self.counted.assert_called_once_with(NEW, "CRM Lead", lead)

	def test_an_emptied_owner_removes_no_one(self):
		"""Emptying the owner is crm's: the owner goes, every assignee stays."""
		lead = self._lead(11)
		bulk_actions_run._assign_row("CRM Lead", lead, [CROWD[1]])
		bulk_actions_run.run_bulk_edit("CRM Lead", [lead], {"field": "lead_owner", "value": ""})
		self.assertFalse(frappe.db.get_value("CRM Lead", lead, "lead_owner"))
		self.assertEqual(self._owners(lead), {OLD, CROWD[1]})

	def test_a_removal_from_the_lead_page_is_silent_and_takes_the_share(self):
		from tatva_connect.access import native_guards

		lead = self._lead(12)
		bulk_actions_run._assign_row("CRM Lead", lead, [CROWD[2]])
		share.add_docshare("CRM Lead", lead, CROWD[2], flags={"ignore_share_permission": True})
		before = self._alerts(CROWD[2], lead)
		native_guards.remove_assignments("CRM Lead", lead, json.dumps([CROWD[2]]))
		self.assertNotIn(CROWD[2], self._owners(lead))
		self.assertFalse(self._shared(lead, CROWD[2]))
		self.assertEqual(self._alerts(CROWD[2], lead), before)

	def test_the_only_person_removed_from_the_lead_page_keeps_no_share(self):
		"""The widget removes the owner, then blanks the owner field (AssignTo.vue:65-79); no share may outlive both."""
		from tatva_connect.access import native_guards

		lead = self._lead(15)
		share.add_docshare("CRM Lead", lead, OLD, flags={"ignore_share_permission": True})
		native_guards.remove_assignments("CRM Lead", lead, json.dumps([OLD]))
		doc = frappe.get_doc("CRM Lead", lead)
		doc.lead_owner = ""
		doc.save()
		self.assertFalse(self._shared(lead, OLD))

	def _owner_moves(self, lead):
		rows = frappe.get_all("Version", filters={"ref_doctype": "CRM Lead", "docname": lead}, pluck="data")
		return [c[1:] for data in rows for c in json.loads(data).get("changed") or [] if c[0] == "lead_owner"]

	def test_an_owner_moved_by_a_managers_assign_is_in_the_history(self):
		lead = self._lead(14)
		frappe.set_user(MANAGER)
		try:
			bulk_actions_run.run_assign("CRM Lead", [lead], {"assign_to": [NEW]})
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "lead_owner"), NEW)
		self.assertIn([OLD, NEW], self._owner_moves(lead))

	def test_assign_still_adds_beside_the_current_holder(self):
		"""Assign is unchanged: it adds a holder and never removes one."""
		lead = self._lead(10)
		bulk_actions_run.run_assign("CRM Lead", [lead], {"assign_to": [NEW]})
		self.assertEqual(self._owners(lead), {OLD, NEW})

	def test_the_action_is_legal_at_the_executor_door(self):
		self.assertIn("Reassign", bulk_actions._EXECUTORS)

	def test_the_action_is_legal_at_the_job_door(self):
		"""The queued path inserts this string, and every action queues — a Select missing it fails every time."""
		options = frappe.get_meta("CRM List Action Job").get_field("action").options.split("\n")
		self.assertIn("Reassign", options)

	def test_the_queued_path_actually_accepts_a_reassign_job(self):
		"""Both doors, proven together — the row the seam writes for every reassign."""
		docnames = json.dumps([f"CRM-LEAD-{i:04d}" for i in range(25)])
		out = bulk_actions.run_or_queue("Reassign", "CRM Lead", docnames, json.dumps({"assign_to": [NEW]}))
		self.assertTrue(out["queued"])
		self.assertEqual(frappe.db.get_value("CRM List Action Job", out["job"], "action"), "Reassign")
		frappe.db.rollback()
