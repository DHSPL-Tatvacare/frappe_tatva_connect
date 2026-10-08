# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One credential chain: the app's token, then the Page tokens derived from it. A source's own token is never read.

The source fixture DOES hold a token (the upstream field still exists, and prod sources still carry one), so
every assertion here would pass by accident if any path still borrowed it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_credential_chain
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync import api
from tatva_connect.tests.api import partner_fixture
from tatva_connect.tests.lead_sync import ensure_app

APP = "700000000000003"
PAGE = "zz-chain-page"
FORM = "zz-chain-form"
SOURCE = "zz-chain-src"


class TestCredentialChain(FrappeTestCase):
	def setUp(self):
		ensure_app(APP)
		partner_fixture.mint_grain()
		self.contract = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": SOURCE, "enabled": 1,
			"vertical": partner_fixture.VERTICAL, "crm_group": partner_fixture.GROUP,
		}).insert(ignore_permissions=True).name
		frappe.get_doc({
			"doctype": "Facebook Page", "id": PAGE, "page_name": "ZZ Chain Page",
			"category": "Test", "account_id": "zz-account",
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Facebook Lead Form", "id": FORM, "page": PAGE, "form_name": FORM,
		}).insert(ignore_permissions=True)
		self.source = frappe.get_doc({
			"doctype": "Lead Sync Source", "facebook_app": APP, "name": SOURCE, "type": "Facebook",
			"access_token": "zz-source-token", "facebook_lead_form": FORM, "api_mapping": self.contract,
			"background_sync_frequency": "Daily", "enabled": 1,
		}).insert(ignore_permissions=True)

	def tearDown(self):
		for doctype, name in (("Lead Sync Source", SOURCE), ("Facebook Lead Form", FORM), ("Facebook Page", PAGE)):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.delete_doc("CRM Lead API Mapping", self.contract, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM Facebook App", APP, force=True, ignore_permissions=True, ignore_missing=True)
		partner_fixture.teardown()

	def _give_app_a_token(self, value):
		with patch("tatva_connect.lead_sync.token.token_info", return_value={"expires_at": 0}):
			app = frappe.get_doc("CRM Facebook App", APP)
			app.access_token = value
			app.save(ignore_permissions=True)

	def test_refresh_refuses_without_an_app_token_instead_of_borrowing_a_sources(self):
		"""A source's token was picked by row order, so a new source holding none broke Refresh for every Page."""
		with patch.object(api, "fetch_and_store_pages", return_value=[]) as discover:
			with self.assertRaises(frappe.ValidationError):
				api.refresh_app(APP)
		discover.assert_not_called()

	def test_the_crawl_falls_back_to_the_app_token_never_the_sources(self):
		self.assertFalse(self.source.crawl_token(), "with no Page or app token there is nothing to crawl with")
		self._give_app_a_token("zz-app-token")
		self.assertEqual(self.source.crawl_token(), "zz-app-token")

	def test_the_page_token_wins_over_the_app_token(self):
		self._give_app_a_token("zz-app-token")
		page = frappe.get_doc("Facebook Page", PAGE)
		page.access_token = "zz-page-token"
		page.save(ignore_permissions=True)
		self.assertEqual(self.source.crawl_token(), "zz-page-token")

	def test_saving_a_system_user_token_stores_it_as_pasted_with_no_expiry(self):
		"""Graph reports expires_at 0 for a system user token: nothing to exchange, nothing to alert on."""
		with patch("tatva_connect.lead_sync.token.exchange_for_long_lived",
		           side_effect=AssertionError("a non-expiring token must not be exchanged")):
			self._give_app_a_token("zz-system-user-token")
		app = frappe.get_doc("CRM Facebook App", APP)
		self.assertEqual(app.get_password("access_token"), "zz-system-user-token")
		self.assertIsNone(app.token_expires_on)
