# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The form name is the campaign's default, not its value.

Meta prefills the real campaign onto a submission from the ad URL. The fold stamped the FORM NAME over
`acq:utm_campaign` after reading the answers, so a form that mapped the campaign had its answer replaced.
On prod the two never once agreed — 0 of 2,318 leads: the column read "GLP-1 - v4 - 220526" while the
campaign the click actually arrived with was "GLP 1st In-lead campaign".

Both directions are held here: a form that maps it keeps what the ad said, and a form that does not is
unchanged and still gets the form name.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_campaign_default
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync.form import IDENTITY_KEY
from tatva_connect.lead_sync.source import TatvaFacebookSyncSource
from tatva_connect.tests.api import partner_fixture
from tatva_connect.tests.lead_sync import ensure_app

FORM_NAME = "ZZ Campaign Form"
AD_CAMPAIGN = "GLP 1st In-lead campaign"


class TestCampaignDefault(FrappeTestCase):
	PAGE = "zz-campaign-page"
	FORM = "zz-campaign-form"
	SOURCE = "zz-campaign-src"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		partner_fixture.mint_grain()
		cls.contract = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": cls.SOURCE, "enabled": 1,
			"is_internal": 0, "vertical": partner_fixture.VERTICAL, "crm_group": partner_fixture.GROUP,
		}).insert(ignore_permissions=True).name
		if not frappe.db.exists("Facebook Page", cls.PAGE):
			frappe.get_doc({
				"doctype": "Facebook Page", "id": cls.PAGE, "page_name": "ZZ Campaign Page",
				"category": "Test", "access_token": "zz-token", "account_id": "zz-account",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Facebook Lead Form", cls.FORM):
			frappe.get_doc({
				"doctype": "Facebook Lead Form", "id": cls.FORM, "page": cls.PAGE,
				"form_name": FORM_NAME,
				"questions": [{"key": "q_phone", "label": "Phone", "mapped_to_crm_field": IDENTITY_KEY}],
			}).insert(ignore_permissions=True)
		with patch("tatva_connect.lead_sync.source.fetch_and_store_pages", return_value=[]), \
		     patch("tatva_connect.lead_sync.source.refresh_credential", return_value=None):
			if not frappe.db.exists("Lead Sync Source", cls.SOURCE):
				frappe.get_doc({
					"doctype": "Lead Sync Source", "facebook_app": ensure_app(), "name": cls.SOURCE,
					"type": "Facebook", "access_token": "zz-token", "facebook_lead_form": cls.FORM,
					"api_mapping": cls.contract, "background_sync_frequency": "Daily", "enabled": 0,
				}).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		frappe.db.delete("Failed Lead Sync Log", {"source": cls.SOURCE})
		frappe.delete_doc("Lead Sync Source", cls.SOURCE, force=True, ignore_permissions=True)
		frappe.delete_doc("Facebook Lead Form", cls.FORM, force=True, ignore_permissions=True)
		frappe.delete_doc("Facebook Page", cls.PAGE, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM Lead API Mapping", cls.contract, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610006%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge()
		self._map_campaign(None)
		frappe.db.commit()

	def _map_campaign(self, field_key):
		"""Add or clear the form's `utm campaign` question — the operator's edit, in one place."""
		doc = frappe.get_doc("Facebook Lead Form", self.FORM)
		doc.questions = [q for q in doc.questions if q.key != "utm campaign"]
		if field_key:
			doc.append("questions", {"key": "utm campaign", "label": "utm campaign",
			                         "mapped_to_crm_field": field_key})
		doc.save(ignore_permissions=True)
		frappe.db.commit()

	def _crawl(self, phone):
		lead = {
			"id": f"zz-campaign-{phone[-4:]}",
			"created_time": "2026-09-02T09:00:00+0530",
			"field_data": [
				{"name": "q_phone", "values": [phone]},
				{"name": "utm campaign", "values": [AD_CAMPAIGN]},
			],
		}
		fold = TatvaFacebookSyncSource("zz-token", self.FORM, source_name=self.SOURCE)
		with patch.object(TatvaFacebookSyncSource, "fetch_leads", return_value=[lead]):
			fold.sync()
		doc = frappe.get_doc("CRM Lead", {"facebook_lead_id": lead["id"]})
		return [r.utm_campaign for r in doc.custom_acquisition_profile]

	def test_a_mapped_campaign_keeps_what_the_ad_said(self):
		self._map_campaign("acq:utm_campaign")
		self.assertEqual(self._crawl("+916100060001"), [AD_CAMPAIGN])

	def test_an_unmapped_campaign_still_gets_the_form_name(self):
		"""The default is untouched: a form that maps nothing behaves exactly as it did before."""
		self._map_campaign(None)
		self.assertEqual(self._crawl("+916100060002"), [FORM_NAME])
