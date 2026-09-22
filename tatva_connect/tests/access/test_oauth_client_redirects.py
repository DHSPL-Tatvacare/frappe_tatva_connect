# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A client that registers several redirect URIs can still authorize with one of them.

The oracle is frappe's OWN validator, not a string comparison of ours: `OAuthWebRequestValidator`
is what the authorize endpoint asks, so a test that asks it too goes red the day our normalisation
stops matching what frappe reads. The evasion is written down as well — the same row stored the way
frappe's registration writes it, which is the shape that fails.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_oauth_client_redirects
"""
import frappe
from frappe.oauth import OAuthWebRequestValidator, get_url_delimiter
from frappe.tests.utils import FrappeTestCase

LIVE = "https://oauth-redirect.googleusercontent.com/r/tatva-test"
SANDBOX = "https://oauth-redirect-sandbox.googleusercontent.com/r/tatva-test"


class TestOAuthClientRedirects(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def _client(self, redirect_uris):
		doc = frappe.get_doc({
			"doctype": "OAuth Client", "app_name": "zz-tatva-test", "redirect_uris": redirect_uris,
			"default_redirect_uri": LIVE, "grant_type": "Authorization Code", "response_type": "Code",
			"scopes": "all",
		}).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "OAuth Client", doc.name, force=True, ignore_permissions=True)
		return doc

	def test_every_registered_uri_is_accepted_by_frappes_own_validator(self):
		doc = self._client(f"{LIVE}\n{SANDBOX}")
		validator = OAuthWebRequestValidator()
		for uri in (LIVE, SANDBOX):
			self.assertTrue(validator.validate_redirect_uri(doc.client_id, uri, None),
			                f"frappe would refuse {uri}, which this client registered")

	def test_the_stored_shape_is_the_one_frappe_splits_on(self):
		self.assertEqual(self._client(f"{LIVE}\n{SANDBOX}").redirect_uris,
		                 get_url_delimiter().join([LIVE, SANDBOX]))

	def test_a_newline_separated_row_is_what_frappe_refuses(self):
		"""The evasion: stored as frappe's own registration writes it, the same URI no longer matches."""
		doc = self._client(LIVE)
		frappe.db.set_value("OAuth Client", doc.name, "redirect_uris", f"{LIVE}\n{SANDBOX}",
		                    update_modified=False)
		self.assertFalse(OAuthWebRequestValidator().validate_redirect_uri(doc.client_id, LIVE, None),
		                 "the defect this override exists for has gone; the override can go with it")

	def test_one_uri_is_untouched(self):
		self.assertEqual(self._client(LIVE).redirect_uris, LIVE)
