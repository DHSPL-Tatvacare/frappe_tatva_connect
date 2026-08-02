# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Form drift is audible: a source pointing at a form Facebook no longer reports says so.

A duplicated or recreated Facebook form gets a NEW id. The Lead Sync Source still names the old one, so
the crawl asks a dead form for leads, Graph answers zero, and nothing errors — leads stop arriving with
no signal at all. These lock the check that turns that silence into one Failed Lead Sync Log row.

The Graph listing is stubbed (patching `list_forms`, the ONE lister discovery and the drift check share)
so the test asserts OUR behaviour, never Facebook's availability.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_form_drift
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync import drift
from tatva_connect.tests.lead_sync import ensure_app


class TestFormDrift(FrappeTestCase):
	PAGE = "zz-drift-page"
	LIVE_FORM = "zz-drift-form-live"
	DEAD_FORM = "zz-drift-form-dead"
	# Never created and never sourced: what a form duplicated by marketing looks like on the next crawl.
	NEW_FORM = "zz-drift-form-duplicated"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Facebook Page", cls.PAGE):
			frappe.get_doc({
				"doctype": "Facebook Page", "id": cls.PAGE, "page_name": "ZZ Drift Page",
				"category": "Test", "access_token": "zz-token", "account_id": "zz-account",
			}).insert(ignore_permissions=True)
		for form_id in (cls.LIVE_FORM, cls.DEAD_FORM):
			if not frappe.db.exists("Facebook Lead Form", form_id):
				frappe.get_doc({
					"doctype": "Facebook Lead Form", "id": form_id, "page": cls.PAGE,
					"form_name": form_id,
				}).insert(ignore_permissions=True)

		# Real, DORMANT sources: the log's `source` is a Link so a stand-in dict cannot be logged against; discovery is patched out because creating a source must not call Facebook.
		with patch("tatva_connect.lead_sync.source.fetch_and_store_pages", return_value=[]):
			for form_id in (cls.LIVE_FORM, cls.DEAD_FORM):
				name = f"zz-src-{form_id}"
				if not frappe.db.exists("Lead Sync Source", name):
					frappe.get_doc({
						"doctype": "Lead Sync Source", "facebook_app": ensure_app(), "name": name, "type": "Facebook",
						"access_token": "zz-token", "facebook_lead_form": form_id,
						"background_sync_frequency": "Daily", "enabled": 0,
					}).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		for form_id in (cls.LIVE_FORM, cls.DEAD_FORM):
			frappe.db.delete("Failed Lead Sync Log", {"source": f"zz-src-{form_id}"})
			frappe.delete_doc("Lead Sync Source", f"zz-src-{form_id}", force=True, ignore_permissions=True)
			frappe.delete_doc("Facebook Lead Form", form_id, force=True, ignore_permissions=True)
		frappe.delete_doc("Facebook Page", cls.PAGE, force=True, ignore_permissions=True)
		# The drift log commits, so these fixtures outlive the case rollback and the removal must commit too.
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		# Each test asserts the logs IT caused; the drift log is a real insert that outlives a rollback.
		for form_id in (self.LIVE_FORM, self.DEAD_FORM):
			frappe.db.delete("Failed Lead Sync Log", {"source": f"zz-src-{form_id}"})

	def _source(self, form_id):
		"""The real source row; the check reads .name and .facebook_lead_form off it."""
		return frappe.get_doc("Lead Sync Source", f"zz-src-{form_id}")

	def _logs_for(self, source_name):
		return frappe.get_all(
			"Failed Lead Sync Log", filters={"source": source_name, "type": drift.LOG_TYPE_MISSING}, pluck="name"
		)

	def _logs_of_type(self, source_name, log_type):
		return frappe.get_all(
			"Failed Lead Sync Log", filters={"source": source_name, "type": log_type}, pluck="name"
		)

	def test_a_live_form_that_no_source_crawls_is_reported(self):
		"""The duplicated-form case, which the id check cannot see.

		Duplicating is the ordinary way to edit a published form: it mints a NEW id and leaves the original
		live. So the configured id is still in the listing, the id check passes, and the only sign that
		submissions have moved is a form on the Page that nobody crawls."""
		src = self._source(self.LIVE_FORM)
		listing = [{"id": self.LIVE_FORM}, {"id": self.NEW_FORM}]
		with patch.object(drift, "list_forms", return_value=listing):
			self.assertTrue(drift.report_form_drift(src))
		logs = self._logs_of_type(src.name, drift.LOG_TYPE_UNSOURCED)
		self.assertEqual(len(logs), 1, "one log per crawl, not one per form")
		payload = frappe.parse_json(frappe.get_doc("Failed Lead Sync Log", logs[0]).lead_data)
		self.assertEqual(payload["unsourced_forms"], [self.NEW_FORM])

	def test_a_page_whose_forms_are_all_crawled_reports_nothing(self):
		"""Both fixture forms carry a source, so a listing of exactly those two is not drift."""
		src = self._source(self.LIVE_FORM)
		with patch.object(drift, "list_forms", return_value=[{"id": self.LIVE_FORM}, {"id": self.DEAD_FORM}]):
			self.assertFalse(drift.report_form_drift(src))
		self.assertEqual(self._logs_of_type(src.name, drift.LOG_TYPE_UNSOURCED), [])

	def test_a_dead_form_is_reported_once(self):
		src = self._source(self.DEAD_FORM)
		with patch.object(drift, "list_forms", return_value=[{"id": self.LIVE_FORM}]):
			self.assertTrue(drift.report_form_drift(src))
		logs = self._logs_for(src.name)
		self.assertEqual(len(logs), 1, "drift must raise exactly one log per crawl, not one per lead")
		row = frappe.get_doc("Failed Lead Sync Log", logs[0])
		payload = frappe.parse_json(row.lead_data)
		self.assertEqual(payload["configured_form"], self.DEAD_FORM)
		self.assertIn(self.LIVE_FORM, payload["live_forms"])

	def test_a_live_form_reports_nothing(self):
		src = self._source(self.LIVE_FORM)
		with patch.object(drift, "list_forms", return_value=[{"id": self.LIVE_FORM}]):
			self.assertFalse(drift.report_form_drift(src))
		self.assertEqual(self._logs_for(src.name), [])

	def test_an_empty_listing_is_not_treated_as_drift(self):
		"""Graph returning nothing is a permission/outage shape, not proof the form is gone — never cry drift on it."""
		src = self._source(self.DEAD_FORM)
		with patch.object(drift, "list_forms", return_value=[]):
			self.assertFalse(drift.report_form_drift(src))
		self.assertEqual(self._logs_for(src.name), [])

	def test_a_broken_check_never_breaks_the_crawl(self):
		"""The check is a diagnostic: if listing raises, the crawl still runs."""
		src = self._source(self.DEAD_FORM)
		with patch.object(drift, "list_forms", side_effect=RuntimeError("graph down")):
			self.assertFalse(drift.report_form_drift(src))

	def test_the_real_token_reaches_the_listing(self):
		"""access_token is flipped to Password by this app, so the column holds a mask and only the Auth row
		holds the secret. Reading the column sends "****" to Graph, every check 400s, and the failure is
		swallowed — the feature silently does nothing. This asserts what actually goes on the wire."""
		seen = {}

		def _spy(page_id, token):
			seen["page"], seen["token"] = page_id, token
			return [{"id": self.LIVE_FORM}]

		with patch.object(drift, "list_forms", _spy):
			drift.report_form_drift(self._source(self.DEAD_FORM))
		self.assertEqual(seen.get("page"), self.PAGE)
		self.assertEqual(seen.get("token"), "zz-token", "the decrypted token must reach Graph, not the mask")
		self.assertNotIn("*", seen.get("token") or "")
