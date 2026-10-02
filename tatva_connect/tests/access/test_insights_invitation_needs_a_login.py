# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An Insights invitation may not mint a login on this CRM; the address must already be a live login.
Tests insert the document, not the button's endpoint, because the generic API can insert one too."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect import hooks

DOCTYPE = "Insights User Invitation"
OUTSIDER = "zz-invite-outsider@gmail.com"
COLLEAGUE = "zz-invite-colleague@tatvacare.in"
RETIRED = "zz-invite-retired@tatvacare.in"


def _user(email, enabled):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			doctype="User", email=email, first_name=email.split("@")[0], send_welcome_email=0, user_type="System User"
		).insert(ignore_permissions=True)
	frappe.db.set_value("User", email, "enabled", enabled)
	return email


def _invite(email):
	return frappe.get_doc({"doctype": DOCTYPE, "email": email}).insert(ignore_permissions=True)


class TestInsightsInvitationNeedsALogin(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_user(COLLEAGUE, 1)
		_user(RETIRED, 0)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete(DOCTYPE, {"email": ("in", [OUTSIDER, COLLEAGUE, RETIRED])})

	def test_the_override_is_wired(self):
		"""A misspelt hooks path leaves upstream's class serving every request and every other test here green."""
		self.assertEqual(
			hooks.override_doctype_class.get(DOCTYPE),
			"tatva_connect.access.insights_invitation.TatvaInsightsUserInvitation",
		)
		self.assertIsInstance(frappe.get_doc({"doctype": DOCTYPE, "email": COLLEAGUE}), frappe.get_attr(hooks.override_doctype_class[DOCTYPE]))

	def test_an_address_with_no_login_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_invite(OUTSIDER)
		self.assertFalse(frappe.db.exists("User", OUTSIDER), "the refused invitation still minted a User")
		self.assertFalse(frappe.db.exists(DOCTYPE, {"email": OUTSIDER}), "the refused invitation was still stored")

	def test_a_disabled_account_is_refused(self):
		"""Otherwise an invitation silently restores access to an account someone deliberately revoked."""
		with self.assertRaises(frappe.ValidationError):
			_invite(RETIRED)

	def test_a_colleague_who_already_signs_in_is_invited_normally(self):
		"""The gate must not break the only legitimate use of the button."""
		invite = _invite(COLLEAGUE)
		self.assertEqual(invite.email, COLLEAGUE)
		self.assertEqual(invite.status, "Pending")
		self.assertTrue(invite.key, "upstream's before_insert did not run — super() was not called")

	def test_case_and_whitespace_do_not_defeat_the_lookup(self):
		"""A pasted address in any case with padding still finds the login, and is stored trimmed.
		`accept()` keys user creation off it, so a padded address would mint a duplicate."""
		invite = _invite(f"  {COLLEAGUE.upper()}  ")
		self.assertTrue(invite.key)
		self.assertEqual(invite.email, COLLEAGUE.upper(), "the address was stored padded")
