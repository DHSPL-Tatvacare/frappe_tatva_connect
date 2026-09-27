# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One authoring gate: the ledger's DocPerms decide Workflows and task forms, asked as a real user per role.

What is asserted, for a user holding exactly one role (each role either doctype declares, plus none):

  * the Workflows surface equals `has_permission("CRM Workflow", "read")` for that user — the permission
    is the whole gate, so the surface can never widen or narrow it (no switch, no role literal);
  * `CRM Task Type` read/write/create/delete equal what `ledger.rows_for` declares for that role — the
    live matrix is the declaration, so a Desk edit or a missed migrate goes red here.

Needs the ledger applied (`bench migrate`), since it reads the live matrix, not the declaration.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_one_authoring_gate
"""
# Replaces test_surface_gates.py (archived 2026-09-25 as .archive/tests/access/archived_test_surface_gates.py — not test_*, or the runner imports it): mocked gates, and a teardown that left orphan roles.
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import ledger
from tatva_connect.access.surfaces import my_surfaces

WORKFLOW = "CRM Workflow"
TASK_TYPE = "CRM Task Type"
PTYPES = ("read", "write", "create", "delete")
ROLES = sorted(set(ledger.rows_for(WORKFLOW)) | set(ledger.rows_for(TASK_TYPE)))


def _email(role):
	return f"zz-gate-{frappe.scrub(role or 'none')}@example.test"


class TestOneAuthoringGate(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for role in [*ROLES, None]:
			if frappe.db.exists("User", _email(role)):
				continue
			user = frappe.get_doc({
				"doctype": "User", "email": _email(role), "first_name": f"ZZ Gate {role or 'None'}",
				"send_welcome_email": 0,
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
			if role:
				user.add_roles(role)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for role in [*ROLES, None]:
			frappe.delete_doc("User", _email(role), force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")

	def test_the_workflows_surface_is_the_workflow_read_permission(self):
		"""Per role, the menu answers exactly what the permission engine answers — and both answers occur."""
		seen = set()
		for role in [*ROLES, None]:
			with self.subTest(role=role):
				frappe.set_user(_email(role))
				permitted = bool(frappe.has_permission(WORKFLOW, "read"))
				self.assertEqual(my_surfaces()["workflows"], permitted,
								 f"the Workflows surface disagreed with CRM Workflow read for role {role}")
				seen.add(permitted)
		self.assertEqual(seen, {True, False}, "fixture: every role got the same answer, so nothing was compared")

	def test_task_type_matrix_is_the_ledger(self):
		"""Each role holds on CRM Task Type exactly the rights the ledger declares; a role it omits holds none."""
		declared = ledger.rows_for(TASK_TYPE)
		for role in [*ROLES, None]:
			flags = declared.get(role, (0, 0, 0, 0))
			frappe.set_user(_email(role))
			for ptype, flag in zip(PTYPES, flags, strict=True):
				with self.subTest(role=role, ptype=ptype):
					self.assertEqual(bool(frappe.has_permission(TASK_TYPE, ptype)), bool(flag),
									 f"{role} {ptype} on {TASK_TYPE} differs from the ledger")
