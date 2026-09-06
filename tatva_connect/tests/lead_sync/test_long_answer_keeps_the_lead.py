# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A patient who types too much into a box is still a patient.

`_validate_length` (frappe base_document.py:1216) refuses the WHOLE record when a varchar field is one
character over its column. `custom_city` is a `Data` field, so its column is 140 — and City on a Facebook
form is free text. Five leads were lost in the sixteen days after go-live: a Tamil sentence about a
Kodaikanal bus route, a keyboard mash, and a pasted ad URL with its fbclid. Each cost a name, a phone
number and a disease, for a city nobody would have read.

The three payloads below are those three shapes. The last two tests are the ones that matter as much:
a normal answer must be untouched, and a `Small Text` field — which is a `text` column and cannot
overflow — must never be trimmed, or the disease this whole exercise was about would start losing its
tail the day someone writes a long one.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_long_answer_keeps_the_lead
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync.form import IDENTITY_KEY
from tatva_connect.lead_sync.source import TatvaFacebookSyncSource
from tatva_connect.tests.api import partner_fixture
from tatva_connect.tests.lead_sync import ensure_app

CITY_MAX = 140

TAMIL = ("கொடைக்கானல் plan இருந்தா இந்த route try பண்ணுங்க most dangerous route to Kodaikanal "
         "worth ah இருக்கும் Bus போக்குவரத்து இந்த பகுதியில் கிடையாது மிகவும் ஆபத்தானது")
MASH = "iihiijhihihhihihijihjiijjihihiiihiiiiiiihihiihgiiji ii ihiiiiihi ii iiiiiihihiihiiiiihhihihiiiihi hi ijijhiiihhih ii ihiihhiiiihiiihijjijhijjihihihuiiiiiiihii"
URL = ("449&utm_theme=ULIP&utm_term=Savings_Video_WhitlerAiSalariedMen4_AI_English_AIOthers_P2C_"
       "Below15_20k3.9Cr_Wealth_AMLOSPPDEFundII_14052026&fbclid=PAb21jcAUDYmRwZG9mAmV4dG4DYWVtATAA"
       "YWRpZAGrN0Mpufe&utm_id=120243918038630062")


class TestLongAnswerKeepsTheLead(FrappeTestCase):
	PAGE = "zz-long-page"
	FORM = "zz-long-form"
	SOURCE = "zz-long-src"

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
				"doctype": "Facebook Page", "id": cls.PAGE, "page_name": "ZZ Long Page",
				"category": "Test", "access_token": "zz-token", "account_id": "zz-account",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Facebook Lead Form", cls.FORM):
			# The live forms' own mapping: phone, city, and the prefilled disease.
			frappe.get_doc({
				"doctype": "Facebook Lead Form", "id": cls.FORM, "page": cls.PAGE,
				"form_name": "ZZ Long Form",
				"questions": [
					{"key": "q_phone", "label": "Phone", "mapped_to_crm_field": IDENTITY_KEY},
					{"key": "city", "label": "City", "mapped_to_crm_field": "lead:custom_city"},
					{"key": "utm disease", "label": "utm disease",
					 "mapped_to_crm_field": "acq:utm_disease"},
				],
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
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610007%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge()
		frappe.db.delete("Failed Lead Sync Log", {"source": self.SOURCE})
		frappe.db.commit()

	def _crawl(self, phone, city, disease="GLP-1"):
		lead = {
			"id": f"zz-long-{phone[-4:]}",
			"created_time": "2026-09-06T09:00:00+0530",
			"field_data": [
				{"name": "q_phone", "values": [phone]},
				{"name": "city", "values": [city]},
				{"name": "utm disease", "values": [disease]},
			],
		}
		fold = TatvaFacebookSyncSource("zz-token", self.FORM, source_name=self.SOURCE)
		with patch.object(TatvaFacebookSyncSource, "fetch_leads", return_value=[lead]):
			fold.sync()
		name = frappe.db.get_value("CRM Lead", {"facebook_lead_id": lead["id"]})
		return frappe.get_doc("CRM Lead", name) if name else None

	# -- the patient survives, whatever they typed ----------------------------

	def test_a_long_tamil_sentence_in_city_keeps_the_patient(self):
		doc = self._crawl("+916100070001", TAMIL)
		self.assertIsNotNone(doc, "the lead must exist — this exact payload lost one in production")
		self.assertEqual(doc.mobile_no, "+916100070001")
		self.assertLessEqual(len(doc.custom_city), CITY_MAX)

	def test_a_keyboard_mash_in_city_keeps_the_patient(self):
		doc = self._crawl("+916100070002", MASH)
		self.assertIsNotNone(doc)
		self.assertLessEqual(len(doc.custom_city), CITY_MAX)

	def test_a_pasted_ad_url_in_city_keeps_the_patient(self):
		doc = self._crawl("+916100070003", URL)
		self.assertIsNotNone(doc)
		self.assertLessEqual(len(doc.custom_city), CITY_MAX)

	def test_the_disease_still_lands_when_the_city_was_the_problem(self):
		"""The whole point: the junk field is trimmed, the field that matters is untouched."""
		doc = self._crawl("+916100070004", TAMIL, disease="GLP-1")
		self.assertIsNotNone(doc)
		self.assertEqual([r.utm_disease for r in doc.custom_acquisition_profile], ["GLP-1"])

	def test_no_failure_is_logged_for_a_lead_that_now_lands(self):
		self._crawl("+916100070005", MASH)
		self.assertEqual(
			frappe.get_all("Failed Lead Sync Log",
			               filters={"source": self.SOURCE, "type": "Failure"}, pluck="name"),
			[], "a lead that lands must leave no failure behind",
		)

	# -- and nothing else moves ----------------------------------------------

	def test_a_normal_city_is_untouched(self):
		doc = self._crawl("+916100070006", "Kodaikanal")
		self.assertEqual(doc.custom_city, "Kodaikanal")

	def test_a_city_exactly_at_the_limit_is_untouched(self):
		"""The boundary: 140 is allowed, so 140 must survive whole."""
		exact = "K" * CITY_MAX
		doc = self._crawl("+916100070007", exact)
		self.assertEqual(doc.custom_city, exact)

	def test_a_small_text_field_is_never_trimmed(self):
		"""`utm_disease` is Small Text — a `text` column, which cannot overflow. Trimming it would be a
		silent data loss invented by the fix, in the very field this work was about."""
		long_disease = "GLP-1 " * 40  # 240 chars
		doc = self._crawl("+916100070008", "Chennai", disease=long_disease)
		self.assertEqual([r.utm_disease for r in doc.custom_acquisition_profile], [long_disease])
