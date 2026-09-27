# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Assign to User goes through the same native `assign_to.add`/`remove` as the Create Task auto-assign,
with no `ignore_permissions` of its own — an intake form's session is Guest, who holds no permission on
CRM Lead either, so this node was one Guest-triggered workflow away from the exact same PermissionError.

The engine assigns through frappe's own `ignore_permissions`, as frappe's Assignment Rule does, and never switches user.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.workflow_engine import interpreter, refs
from tatva_connect.workflow_engine.tests import fixtures as fx


class TestAssignToUserAsGuest(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("ToDo", {"reference_type": "CRM Lead", "reference_name": cls.lead.name})
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _node(self):
		return frappe._dict({
			"node_id": "a", "node_type": "Assign to User", "edges": [],
			"config_json": frappe.as_json({
				"assign_mode": "Assign", "assignee_mode": "User", "assign_to_user": "Administrator",
			}),
		})

	def _assign_as_guest(self, in_workflow):
		node = self._node()
		frappe.set_user("Guest")
		frappe.flags.in_workflow = in_workflow
		try:
			return interpreter._run_verb(node, self.lead.name, self.lead, refs.Values(), fx.AXES)
		finally:
			frappe.flags.in_workflow = False
			frappe.set_user("Administrator")

	def test_workflow_triggered_guest_assign_no_longer_raises(self):
		self._assign_as_guest(in_workflow=True)
		self.assertTrue(frappe.db.exists("ToDo", {
			"reference_type": "CRM Lead", "reference_name": self.lead.name,
			"allocated_to": "Administrator", "status": "Open",
		}))

	def test_a_signed_in_callers_login_is_left_whole(self):
		"""Frappe saves `local.session` back as the caller's login after the response; RED when the engine switched user."""
		self.addCleanup(setattr, frappe.local, "session", frappe.local.session)
		login = frappe._dict(user="Administrator", sid="zz-login-probe", data=frappe._dict(user="Administrator"))
		frappe.local.session = login
		before = frappe.as_json(login)
		frappe.flags.in_workflow = True
		try:
			interpreter._run_verb(self._node(), self.lead.name, self.lead, refs.Values(), fx.AXES)
		finally:
			frappe.flags.in_workflow = False
		self.assertIs(frappe.local.session, login)
		self.assertEqual(frappe.as_json(login), before)
