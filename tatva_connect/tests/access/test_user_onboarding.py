# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""User Onboarding reads the grain off the user's own User Permissions: none is Not ready at step 3, one is a row on that grain."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access.report.user_onboarding import user_onboarding

V = "ZZ Onboarding Vertical"
USER = "zz-onboarding.rep@example.test"


class TestUserOnboarding(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("CRM Vertical", V):
			frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": V}).insert(ignore_permissions=True)
		if not frappe.db.exists("User", USER):
			frappe.get_doc({"doctype": "User", "email": USER, "first_name": "Onboarding", "send_welcome_email": 0,
			                "roles": [{"role": "Sales User"}, {"role": "Wiki User"}]}).insert(ignore_permissions=True)

	def setUp(self):
		frappe.db.savepoint("onboarding")

	def tearDown(self):
		frappe.db.rollback(save_point="onboarding")
		frappe.cache.hdel("user_permissions", USER)  # a rollback fires no hook, so drop the cached copy

	def _rows(self):
		return user_onboarding.execute({"users": [USER]})[1]

	def test_no_user_permission_is_not_ready_at_step_three(self):
		[row] = self._rows()
		self.assertEqual((row["grain"], row["roles"], row["permissions"], row["verdict"]), ("-", "Done", "Missing", "Not ready"))

	def test_a_user_permission_is_a_row_on_that_grain(self):
		frappe.get_doc({"doctype": "User Permission", "user": USER, "allow": "CRM Vertical", "for_value": V,
		                "applicable_for": "CRM Lead", "apply_to_all_doctypes": 0}).insert(ignore_permissions=True)
		[row] = self._rows()
		self.assertEqual((row["grain"], row["permissions"]), (f"{V} / * / *", "Done"))
