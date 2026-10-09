# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Sales Manager with Automation Manager sets up a whole grain; only a partner login's API keys need a System Manager."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.tests.authz.base import mk_user, set_user

TAG = "setup-roles"


class TestSetupRoles(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.manager = mk_user(f"{TAG}-sm@example.test", ["Sales Manager"])
		cls.author = mk_user(f"{TAG}-am@example.test", ["Sales Manager", "Automation Manager"])

	def _new_login(self, user, email, roles):
		"""Create a login through the User form's own save, as `user`."""
		with set_user(user):
			doc = frappe.get_doc({"doctype": "User", "email": email, "first_name": TAG, "send_welcome_email": 0,
			                      "roles": [{"role": r} for r in roles]})
			doc.insert()
		return doc

	def test_a_sales_manager_creates_a_partner_login(self):
		self._new_login(self.manager, f"{TAG}-partner@example.test", ["Partner API User"])
		self.assertIn("Partner API User", frappe.get_roles(f"{TAG}-partner@example.test"))

	def test_a_sales_manager_never_reaches_the_api_keys(self):
		self.assertEqual(frappe.get_meta("User").get_field("api_key").permlevel, 2)
		self.assertNotIn(2, frappe.get_meta("User").get_permlevel_access("write", user=self.manager))

	def test_an_automation_manager_grants_whatsapp_user(self):
		self._new_login(self.author, f"{TAG}-rep@example.test", ["Sales User", "WhatsApp User"])
		self.assertIn("WhatsApp User", frappe.get_roles(f"{TAG}-rep@example.test"))

	def test_a_sales_manager_alone_cannot_grant_whatsapp_user(self):
		with self.assertRaises(frappe.PermissionError):
			self._new_login(self.manager, f"{TAG}-rep2@example.test", ["Sales User", "WhatsApp User"])

	def test_an_automation_manager_owns_both_channel_accounts(self):
		for doctype in ("WhatsApp Account", "CRM WhatsApp Routing", "CRM Telephony Account", "CRM Telephony Routing"):
			with self.subTest(doctype=doctype):
				self.assertTrue(frappe.has_permission(doctype, "write", user=self.author))
				self.assertFalse(frappe.has_permission(doctype, "write", user=self.manager))

	def test_an_automation_manager_passes_the_whatsapp_admin_gate(self):
		from tatva_connect.whatsapp.webhook import webhook_urls

		with set_user(self.author):
			self.assertIsInstance(webhook_urls(), dict)
