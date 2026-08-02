# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Facebook question maps to a catalog field_key its grain owns — offered, and enforced.

`mapped_to_crm_field` is a free-text Autocomplete on the fork doctype: nothing structurally binds it to
the brain, so an operator could name a target the catalog never declared. The question would then be
collected from the patient and dropped at ingestion with nothing said. The picker offers only the
contract's ticked keys; the save check is the backstop for an API/import write.

Also locked here: upstream's mandatory-field check compares BARE fieldnames ("first_name") against this
column, which since Phase B holds catalog KEYS ("lead:first_name") — so it can never match and throws on
every edit of a mapped form. The override replaces it with the identity key ingestion actually needs.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_form_mapping
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync import api as lead_sync_api
from tatva_connect.lead_sync.form import IDENTITY_KEY
from tatva_connect.tests.api import partner_fixture
from tatva_connect.tests.lead_sync import ensure_app


class TestFacebookFormMapping(FrappeTestCase):
	PAGE = "zz-map-page"
	FORM = "zz-map-form"
	SOURCE = "zz-map-src"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		partner_fixture.mint_grain()
		cls.ticked = partner_fixture.mint_catalog_row("zz_mapped_field")
		if not frappe.db.exists("CRM Lead API Mapping", {"contract_name": cls.SOURCE}):
			frappe.get_doc({
				"doctype": "CRM Lead API Mapping", "contract_name": cls.SOURCE, "enabled": 1,
				"vertical": partner_fixture.VERTICAL, "crm_group": partner_fixture.GROUP,
				"allowed_fields": [{"field": cls.ticked}],
			}).insert(ignore_permissions=True)
		cls.contract = frappe.db.get_value("CRM Lead API Mapping", {"contract_name": cls.SOURCE}, "name")

		if not frappe.db.exists("Facebook Page", cls.PAGE):
			frappe.get_doc({
				"doctype": "Facebook Page", "id": cls.PAGE, "page_name": "ZZ Map Page",
				"category": "Test", "access_token": "zz-token", "account_id": "zz-account",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Facebook Lead Form", cls.FORM):
			frappe.get_doc({
				"doctype": "Facebook Lead Form", "id": cls.FORM, "page": cls.PAGE, "form_name": "ZZ Map Form",
				"questions": [{"key": "q_phone", "label": "Phone"}, {"key": "q_other", "label": "Other"}],
			}).insert(ignore_permissions=True)
		with patch("tatva_connect.lead_sync.source.fetch_and_store_pages", return_value=[]):
			if not frappe.db.exists("Lead Sync Source", cls.SOURCE):
				frappe.get_doc({
					"doctype": "Lead Sync Source", "facebook_app": ensure_app(), "name": cls.SOURCE, "type": "Facebook",
					"access_token": "zz-token", "facebook_lead_form": cls.FORM, "api_mapping": cls.contract,
					"background_sync_frequency": "Daily", "enabled": 0,
				}).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("Lead Sync Source", cls.SOURCE, force=True, ignore_permissions=True)
		frappe.delete_doc("Facebook Lead Form", cls.FORM, force=True, ignore_permissions=True)
		frappe.delete_doc("Facebook Page", cls.PAGE, force=True, ignore_permissions=True)
		if cls.contract:
			frappe.delete_doc("CRM Lead API Mapping", cls.contract, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		super().tearDownClass()

	def _form(self):
		return frappe.get_doc("Facebook Lead Form", self.FORM)

	def _map(self, doc, **by_key):
		for question in doc.questions:
			if question.key in by_key:
				question.mapped_to_crm_field = by_key[question.key]

	# ---- the picker ----------------------------------------------------------
	def test_the_picker_offers_the_contract_ticked_keys(self):
		offered = {f["value"] for f in lead_sync_api.list_mappable_fields(self.FORM)}
		self.assertIn(self.ticked, offered, "a key the contract ticks must be offerable")
		self.assertIn(IDENTITY_KEY, offered, "identity is never optional — allowed_field_keys force-adds it")

	def test_the_picker_offers_nothing_without_a_contract(self):
		"""A form no source points at has no grain to judge against — offer nothing rather than everything."""
		self.assertEqual(lead_sync_api.list_mappable_fields("zz-no-such-form"), [])

	def test_every_offered_key_is_really_in_the_catalog(self):
		catalogued = set(frappe.get_all("CRM Lead API Field", pluck="field_key"))
		for field in lead_sync_api.list_mappable_fields(self.FORM):
			self.assertIn(field["value"], catalogued)

	# ---- the save backstop ---------------------------------------------------
	def test_an_uncontracted_key_is_refused_on_save(self):
		doc = self._form()
		self._map(doc, q_phone=IDENTITY_KEY, q_other="lead:zz_never_ticked_by_this_grain")
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_a_contracted_key_saves(self):
		doc = self._form()
		self._map(doc, q_phone=IDENTITY_KEY, q_other=self.ticked)
		doc.save(ignore_permissions=True)
		self.assertEqual(
			{q.mapped_to_crm_field for q in self._form().questions}, {IDENTITY_KEY, self.ticked}
		)

	def test_identity_must_be_mapped(self):
		"""The replacement for upstream's bare-fieldname check: the dedup key, in KEY space."""
		doc = self._form()
		self._map(doc, q_phone=self.ticked, q_other=self.ticked)
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
