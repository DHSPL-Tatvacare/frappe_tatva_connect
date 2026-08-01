# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An Insights invitation may not mint a login on this CRM.

Upstream's invite flow creates a NEW `User` for whatever address is typed, appends the `Insights User`
role and logs the visitor in from an allow_guest link — so an Insights Admin could hand a personal
mailbox a working account here, and `disable_signup` never sees it because nothing goes through sign-up.
`access/insights_invitation.TatvaInsightsUserInvitation` refuses unless the address is already a live
login.

The gate is on `before_insert`, NOT on `insights.api.user.invite_users`, because an Insights Admin also
holds `create` on the doctype and could insert one straight through the generic API. So these tests
insert the DOCUMENT rather than calling the button's endpoint — the button is one caller, the document
is the choke point, and a test that only drove the endpoint would stay green with the side door open.

`test_the_override_is_wired` is the one that matters most: every other test here imports our class
directly and would keep passing if the hooks entry were removed or misspelled, leaving upstream's class
serving every real request.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_insights_invitation_needs_a_login
"""
import frappe
from frappe.tests.utils import FrappeTestCase

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


class TestInsightsInvitationNeedsALogin(FrappeTestCase):
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
		"""`User.name` collates case-insensitively; a pasted address often carries a trailing space. The
		stored value must come out TRIMMED — `accept()` keys `create_user_if_not_exists` off it, so a
		padded address would miss the real account and mint the duplicate this whole gate exists to stop."""
		invite = _invite(f"  {COLLEAGUE.upper()}  ")
		self.assertTrue(invite.key)
		self.assertEqual(invite.email, COLLEAGUE.upper(), "the address was stored padded")
