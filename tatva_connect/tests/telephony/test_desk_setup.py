# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Desk setup an operator runs: Validate Token on an account, and Fetch Extensions on a rep. Only Acefone's HTTP is stubbed, in its real `/v1/users` shape.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.telephony.test_desk_setup
"""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.telephony import api, discovery
from tatva_connect.tests.telephony.fixtures import config

OWNER = "_test_tel_owner@example.com"
SETUP_MANAGER = "_test_tel_sales_manager@example.com"
OFF_ACCOUNT = "_TestTelephonyAcctOff"
MINE, THEIRS = "0600000000921", "0600000000922"


def _users(*extensions):
	"""Acefone's `/v1/users` answer: one login per extension, with its follow-me phone."""
	return {"data": [{"extension": e, "agent": {"id": f"05{e[2:]}", "name": "Rep", "follow_me_number": f"+9190002009{e[-2:]}", "status": 0}} for e in extensions]}


class TestDeskSetup(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		config.ensure_account(rep_emails=(OWNER,))
		config.ensure_account(account=config.SIBLING_ACCOUNT, token=f"{config.ACCOUNT}-token")

	def setUp(self):
		config.clear_seats()
		config.clear_seats(config.SIBLING_ACCOUNT)

	def tearDown(self):
		config.clear_seats()
		config.clear_seats(config.SIBLING_ACCOUNT)

	def test_a_sales_manager_who_cannot_read_accounts_still_gets_them_to_fetch_a_reps_extension(self):
		if not frappe.db.exists("User", SETUP_MANAGER):
			frappe.get_doc({"doctype": "User", "email": SETUP_MANAGER, "first_name": "Setup", "send_welcome_email": 0,
			                "roles": [{"role": "Sales Manager"}]}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator
		config.set_seat(OWNER, MINE)
		frappe.set_user(SETUP_MANAGER)
		self.addCleanup(frappe.set_user, "Administrator")
		self.assertFalse(frappe.has_permission("CRM Telephony Account", "read"))
		self.assertIn(config.ACCOUNT, discovery.accounts_for("CRM Telephony Agent", OWNER))

	def _enable(self, account, on):
		doc = frappe.get_doc("CRM Telephony Account", account)
		doc.enabled = on
		doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator

	def test_a_disabled_account_is_never_asked_even_when_named_directly(self):
		config.ensure_account(account=OFF_ACCOUNT)
		self._enable(OFF_ACCOUNT, 0)
		self.addCleanup(self._enable, OFF_ACCOUNT, 1)
		with patch.object(api, "make_get_request") as acefone_http:
			with self.assertRaisesRegex(frappe.ValidationError, "not an enabled telephony account"):
				discovery.extensions_for_agent("Administrator", OFF_ACCOUNT)
		acefone_http.assert_not_called()
		self.assertNotIn(OFF_ACCOUNT, discovery.accounts_for("CRM Telephony Agent", "Administrator"))

	def test_validate_token_names_the_accounts_sharing_the_provider_login(self):
		with patch.object(api, "make_get_request", return_value=_users(MINE, THEIRS)):
			report = discovery.validate_token("CRM Telephony Account", config.ACCOUNT)
		checks = {c["label"]: c for c in report["checks"]}
		self.assertTrue(checks["Accepted by Acefone"]["passed"])
		self.assertEqual(checks["Extensions"]["detail"], "2")
		self.assertIn(config.SIBLING_ACCOUNT, checks["Same provider login as"]["detail"])

	def test_fetch_extensions_offers_the_reps_own_seat_and_marks_one_held_on_a_sibling_account(self):
		config.set_seat("Administrator", MINE)
		config.set_seat(OWNER, THEIRS, account=config.SIBLING_ACCOUNT)
		with patch.object(api, "make_get_request", return_value=_users(MINE, THEIRS)):
			rows = {r["name"]: r for r in discovery.extensions_for_agent("Administrator", config.ACCOUNT)["rows"]}
		self.assertEqual((rows[MINE]["taken"], rows[MINE]["taken_by"]), (False, "Administrator"))
		self.assertEqual((rows[THEIRS]["taken"], rows[THEIRS]["taken_by"]), (True, OWNER))
		self.assertEqual(rows[MINE]["follow_me_number"], "+919000200921")
