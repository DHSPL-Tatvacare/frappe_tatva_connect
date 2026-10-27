# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A separation hands an owned, assigned lead to the new user as owner AND assignee, takes the departing user out of a
Credit Weighted pool's `weighted_users` grid while the rule stays enabled, and disables the account last."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import separation
from tatva_connect.lead import assignment
from tatva_connect.workflow_engine.tests.fixtures import GRAIN, make_lead, make_pool

LEAVER = "zz-separation-leaver@example.test"
SUCCESSOR = "zz-separation-successor@example.test"
PHONE = "+916100092001"


class TestUserSeparation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		for email in (LEAVER, SUCCESSOR):
			frappe.get_doc({"doctype": "User", "email": email, "first_name": "Separation", "send_welcome_email": 0,
			                "roles": [{"role": "Sales User"}, {"role": "Wiki User"}]}).insert(ignore_permissions=True)
		# Entitled either way the grain is read: pool membership (registry off) and a User Permission (registry on).
		frappe.get_doc({"doctype": "User Permission", "user": SUCCESSOR, "allow": "CRM Vertical", "for_value": GRAIN["vertical"],
		                "applicable_for": "CRM Lead", "apply_to_all_doctypes": 0}).insert(ignore_permissions=True)
		cls.pool = make_pool([{"user": LEAVER, "weight": 1}, {"user": SUCCESSOR, "weight": 1}], assigned_by_workflow=1).name
		cls.lead = make_lead(mobile_no=PHONE, lead_owner=LEAVER).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("Tatva User Separation", filters={"user": LEAVER}, pluck="name"):
			frappe.delete_doc("Tatva User Separation", name, force=True, ignore_permissions=True)
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": PHONE}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
		for rule in set(frappe.get_all("Assignment Rule User", filters={"user": ["in", (LEAVER, SUCCESSOR)]}, pluck="parent")):
			frappe.delete_doc("Assignment Rule", rule, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"allocated_to": ["in", (LEAVER, SUCCESSOR)]})
		frappe.db.delete("User Permission", {"user": ["in", (LEAVER, SUCCESSOR)]})
		for email in (LEAVER, SUCCESSOR):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)

	def test_one_run_hands_over_owner_and_assignee_and_clears_every_pool_grid(self):
		doc = frappe.get_doc({"doctype": "Tatva User Separation", "user": LEAVER, "reassign_to": SUCCESSOR}).insert()
		self.assertIn(("CRM Lead", SUCCESSOR), {(r.ref_doctype, r.assign_to) for r in doc.work})
		self.assertIn("Remove from assignment rules", {s.step for s in doc.steps})
		self.assertEqual([t for level, t, _fix in separation.review(doc) if level == "red"], [])

		with patch("frappe.enqueue") as queued:  # the long-lane job is run below, in this process
			separation.run(doc.name)
		self.assertEqual(queued.call_args.kwargs["queue"], "long")
		separation.work(doc.name)

		owner = frappe.db.get_value("CRM Lead", self.lead, "lead_owner")
		holders = set(frappe.get_all("ToDo", filters={"reference_type": "CRM Lead", "reference_name": self.lead, "status": "Open"}, pluck="allocated_to"))
		self.assertEqual((owner, holders), (SUCCESSOR, {SUCCESSOR}))
		pool = frappe.get_doc("Assignment Rule", self.pool)
		self.assertEqual(([u.user for u in pool.weighted_users], pool.disabled), ([SUCCESSOR], 0))
		self.assertEqual(frappe.db.get_value("User", LEAVER, "enabled"), 0)
		doc.reload()
		self.assertEqual(doc.status, "Completed", [(r.idx, r.status, r.result) for r in [*doc.work, *doc.steps]])

	def test_a_ticket_the_runner_cannot_read_is_refused_not_handed_over(self):
		ticket = frappe.get_doc({"doctype": "HD Ticket", "subject": "Zz separation ticket"}).insert(ignore_permissions=True).name
		assignment.assign("HD Ticket", ticket, LEAVER)
		frappe.db.commit()  # each hand-over row commits or rolls back alone, so the fixture must already stand
		try:
			frappe.set_user(SUCCESSOR)  # a Sales User: no read on HD Ticket
			out = separation._helpdesk_assign("HD Ticket", [ticket], SUCCESSOR, LEAVER)
			frappe.set_user("Administrator")
			self.assertEqual((out["succeeded"], out["failed"]), (0, 1))
			self.assertEqual(assignment.current_assignees("HD Ticket", ticket), [LEAVER])
			out = separation._helpdesk_assign("HD Ticket", [ticket], SUCCESSOR, LEAVER)
			self.assertEqual((out["succeeded"], out["failed"]), (1, 0))
			self.assertEqual(assignment.current_assignees("HD Ticket", ticket), [SUCCESSOR])
		finally:
			frappe.set_user("Administrator")
			frappe.delete_doc("HD Ticket", ticket, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_a_run_whose_job_is_gone_can_be_retried(self):
		doc = frappe.get_doc({"doctype": "Tatva User Separation", "user": LEAVER, "reassign_to": SUCCESSOR}).insert()
		try:
			with patch("frappe.enqueue"):  # the job never reaches RQ, as when a worker dies mid-run
				separation.run(doc.name)
			self.assertTrue(separation.stranded(frappe.get_doc("Tatva User Separation", doc.name)))
			with patch("tatva_connect.access.separation.is_job_enqueued", return_value=True), self.assertRaises(frappe.ValidationError):
				separation.run(doc.name)  # a run RQ still holds stays refused
			with patch("frappe.enqueue") as queued:
				separation.run(doc.name)
			queued.assert_called_once()
		finally:
			frappe.delete_doc("Tatva User Separation", doc.name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_a_pool_the_leaver_would_leave_empty_blocks_the_handover(self):
		lonely = make_pool([{"user": LEAVER, "weight": 1}], assigned_by_workflow=1).name
		doc = frappe.get_doc({"doctype": "Tatva User Separation", "user": LEAVER, "reassign_to": SUCCESSOR}).insert()
		red = [t for level, t, _fix in separation.review(doc) if level == "red"]
		self.assertTrue(any(lonely in t for t in red), red)
		frappe.delete_doc("Tatva User Separation", doc.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Assignment Rule", lonely, force=True, ignore_permissions=True)
