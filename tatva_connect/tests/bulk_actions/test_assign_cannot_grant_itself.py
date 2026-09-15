# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Assigning a record is not how you become allowed to see it.

THE DEFECT. crm's row gate grants read wherever a live, non-cancelled ToDo names you
(`crm/permissions/org_hierarchy.py:56-58`). Frappe therefore gates its own assignment door on READ —
`assign_to._add` opens with `frappe.get_doc(doctype, name).check_permission()` (assign_to.py:72), and
`set_status` does the same (assign_to.py:215). This app wrote the `notify=False` branch of `assignment.assign`/`unassign` to
suppress frappe's notification, and dropped that line with it. `bulk_actions.run_or_queue` is whitelisted
and takes `docnames` from the request body, tied to no list the caller was reading — so naming a docname
was enough to assign it to yourself, after which the gate genuinely admitted the record.

The gate is per ROW and inside the executor, where frappe puts it: a record deleted between the queue and
the drain has to fail on its own rather than take the rest of the batch with it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.bulk_actions.test_assign_cannot_grant_itself
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import bulk_actions_run

STRANGER = "zz-bulk-stranger@example.com"
PHONE = "+916100080001"


class TestAssignCannotGrantItself(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		if not frappe.db.exists("User", STRANGER):
			frappe.get_doc({"doctype": "User", "email": STRANGER, "first_name": "Bulk Stranger",
			                "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}
			               ).insert(ignore_permissions=True)
		cls.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Not Theirs", "mobile_no": PHONE, "status": "New",
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		if frappe.db.exists("User", STRANGER):
			frappe.delete_doc("User", STRANGER, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610008%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def _todos(self):
		return frappe.get_all("ToDo", filters={"reference_type": "CRM Lead", "reference_name": self.lead,
		                                       "allocated_to": STRANGER, "status": "Open"})

	def test_a_stranger_cannot_assign_a_lead_they_cannot_read(self):
		"""RED before: the ToDo was written, and the row gate then admitted the lead to them."""
		frappe.set_user(STRANGER)
		try:
			if frappe.has_permission("CRM Lead", "read", doc=self.lead):
				self.skipTest("this bench grants this user the lead outright; nothing to escalate from")
			out = bulk_actions_run.run_assign("CRM Lead", [self.lead], {"assign_to": [STRANGER]})
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(out["succeeded"], 0, "the assignment must not have been written")
		self.assertEqual(self._todos(), [], "no ToDo may exist, or the row gate now admits the lead")

	def test_a_stranger_cannot_strip_someone_elses_assignment(self):
		"""The mirror: Clear Assignment on records you cannot see revokes other people's access."""
		frappe.set_user(STRANGER)
		try:
			if frappe.has_permission("CRM Lead", "read", doc=self.lead):
				self.skipTest("this bench grants this user the lead outright")
			out = bulk_actions_run.run_clear_assignment("CRM Lead", [self.lead], {})
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(out["succeeded"], 0, "the clear must not have run")

	def test_someone_who_may_read_it_still_can(self):
		"""The gate is frappe's READ check, not a new rule — an operator is unaffected."""
		out = bulk_actions_run.run_assign("CRM Lead", [self.lead], {"assign_to": ["Administrator"]})
		self.assertEqual(out["succeeded"], 1)


class TestTheWorkerReAsksTheDoor(FrappeTestCase):
	"""A `CRM List Action Job` row is insertable by any role that may create one, and `after_insert`
	enqueues the drain — so a caller who writes the row instead of calling `run_or_queue` would skip the
	door's rules entirely. `produce_export` re-asks its gate in the worker; this one now does too."""

	def test_a_hand_written_job_over_the_cap_fails_in_the_drain(self):
		from tatva_connect import bulk_actions

		job = frappe.get_doc({
			"doctype": "CRM List Action Job", "action": "Assign", "target_doctype": "CRM Lead",
			"docnames": frappe.as_json([f"zz-{i}" for i in range(bulk_actions.MAX_ROWS + 1)]),
			"params": frappe.as_json({"assign_to": ["Administrator"]}),
			"total": bulk_actions.MAX_ROWS + 1,
		}).insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			bulk_actions.run(job.name)  # RED before: the executor ran on 501 rows
			self.assertEqual(frappe.db.get_value("CRM List Action Job", job.name, "status"), "Error")
		finally:
			frappe.delete_doc("CRM List Action Job", job.name, force=True, ignore_permissions=True)
			frappe.db.commit()
