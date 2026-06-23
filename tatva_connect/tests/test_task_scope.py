# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CRM Task inherits Lead/Deal scope via the shared row-visibility brain (Leg 2).

CRM Task's scope logic was REFACTORED out of tasks/permissions.py into the brain; the parity
test pins it to the frozen legacy logic, but this is the LIVE in-scope/out-of-scope assertion
(matching the three sibling doctypes). With Task::CRM Task::visibility ON: a user who can't see
a lead can't see/open a task on it; the owner/assignee of a task always can (even on a hidden
parent — least-privilege: the task is granted, not the lead). Switch OFF: stock crm.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import visibility
from tatva_connect.automation import seed

_SWITCH = "Task::CRM Task::visibility"


class TestTaskScope(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
		self._set_switch(1)
		self._made = []
		self.in_scope = self._user("task-in@example.com", "Sales User")
		self.out_scope = self._user("task-out@example.com", "Sales User")
		self.lead = self._lead(self.in_scope)
		self._restrict_lead(self.lead, self.in_scope)
		# A task on the lead, NOT owned/assigned to in_scope -> visibility comes purely from the parent.
		self.task = self._task(("CRM Lead", self.lead))

	def tearDown(self):
		frappe.set_user("Administrator")
		for dt, name in reversed(self._made):
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.delete("User Permission", {"allow": "CRM Lead", "for_value": self.lead})
		self._set_switch(0)

	# --- helpers ---------------------------------------------------------
	def _set_switch(self, on):
		frappe.db.set_value("CRM Tatva Automation", _SWITCH, "enabled", 1 if on else 0)

	def _user(self, email, role):
		if not frappe.db.exists("User", email):
			frappe.get_doc({
				"doctype": "User", "email": email, "first_name": email.split("@")[0],
				"send_welcome_email": 0, "roles": [{"role": role}],
			}).insert(ignore_permissions=True)
		return email

	def _lead(self, owner):
		ld = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Scope", "lead_name": "Scope Lead",
			"status": "New", "lead_owner": owner,
		})
		ld.insert(ignore_permissions=True)
		ld.db_set("owner", owner)
		self._made.append(("CRM Lead", ld.name))
		return ld.name

	def _restrict_lead(self, lead, user):
		frappe.get_doc({
			"doctype": "User Permission", "user": user,
			"allow": "CRM Lead", "for_value": lead,
		}).insert(ignore_permissions=True)

	def _task(self, reference, assigned_to=None):
		doc = frappe.new_doc("CRM Task")
		doc.title = "Scope Task"
		doc.status = "Todo"
		doc.reference_doctype, doc.reference_docname = reference
		if assigned_to:
			doc.assigned_to = assigned_to
		doc.insert(ignore_permissions=True)
		self._made.append(("CRM Task", doc.name))
		return frappe.get_doc("CRM Task", doc.name)

	# --- switch ON: scope enforced --------------------------------------
	def test_in_scope_user_can_see(self):
		self.assertTrue(
			visibility.scoped_has_permission(self.task, "read", self.in_scope),
			f"in-scope user denied on {self.task.name}",
		)

	def test_out_of_scope_user_blocked(self):
		self.assertFalse(
			visibility.scoped_has_permission(self.task, "read", self.out_scope),
			f"out-of-scope user saw {self.task.name}",
		)

	def test_out_of_scope_blocked_via_frappe_has_permission(self):
		"""The hook is wired, so frappe.has_permission (what the SPA's task fetch calls) denies too."""
		frappe.set_user(self.out_scope)
		try:
			self.assertFalse(frappe.has_permission("CRM Task", "read", self.task.name))
		finally:
			frappe.set_user("Administrator")

	def test_assignee_sees_own_task_on_hidden_parent(self):
		"""Least-privilege carve-out: a task assigned to a user is theirs even if they can't see
		the parent lead — assigning a task grants the task, not the lead."""
		t = self._task(("CRM Lead", self.lead), assigned_to=self.out_scope)
		self.assertTrue(visibility.scoped_has_permission(t, "read", self.out_scope))

	def test_pqc_scopes_list(self):
		pqc = visibility.scoped_pqc("CRM Task", self.out_scope)
		self.assertIn("`tabCRM Task`.`owner`=", pqc)
		# CRM Task DOES have assigned_to -> that clause must appear (unlike Call Log/Note/WhatsApp).
		self.assertIn("assigned_to", pqc)
		self.assertIn("reference_doctype", pqc)
		self.assertIn("reference_docname", pqc)

	# --- switch OFF: stock crm ------------------------------------------
	def test_switch_off_is_stock(self):
		self._set_switch(0)
		self.assertEqual(visibility.scoped_pqc("CRM Task", self.out_scope), "")
		self.assertTrue(visibility.scoped_has_permission(self.task, "read", self.out_scope))
