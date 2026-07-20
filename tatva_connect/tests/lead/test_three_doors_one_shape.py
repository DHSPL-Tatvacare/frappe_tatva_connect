# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Three leads — partner API, intake form, Facebook — must be INDISTINGUISHABLE in shape.

This is the invariant CLAUDE.md calls "the test that matters", and until now nothing asserted it. Three
suites each drove ONE door and checked what that door produced; no test ever put the three side by side.
That is precisely the blind spot a second brain grows in — a door acquires its own field handling, its
own defaulting, its own child-table habit, and every per-door suite stays green while the doors drift
apart. A rep then sees three different-looking patients and nobody can say why.

Only two things may differ: `source` and `custom_source_origin`, which are the record OF the door. If a
fourth thing differs, a door has started deciding something `_upsert_one` is supposed to decide for all
of them.

All three write the same patient with the same grain, on three phone numbers so the dedup anchor keeps
them apart rather than merging them into one row.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_three_doors_one_shape
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import partner
from tatva_connect.intake import builder, intake
from tatva_connect.lead_sync.form import IDENTITY_KEY
from tatva_connect.lead_sync.source import TatvaFacebookSyncSource
from tatva_connect.tests.api import partner_fixture

PATIENT = "Asha Threedoors"
PHONE_PARTNER = "+916100040001"
PHONE_INTAKE = "+916100040002"
PHONE_FACEBOOK = "+916100040003"

INTAKE_SWITCH = "Lead::Enrolment::intake"

# What a lead is ALLOWED to differ on, and why. Everything else must match across the three doors.
#   * the door's own record of itself — the one documented exception
#   * identity and framework bookkeeping, which are per-row by definition
_MAY_DIFFER = {
	"source", "custom_source_origin",
	"name", "mobile_no", "creation", "modified", "owner", "modified_by", "idx", "docstatus",
	"naming_series", "external_id", "facebook_lead_id", "facebook_form_id",
	"_user_tags", "_comments", "_assign", "_liked_by",
}


class TestThreeDoorsOneShape(FrappeTestCase):
	PAGE = "zz-doors-page"
	FB_FORM = "zz-doors-fb-form"
	SOURCE = "zz-doors-src"
	INTAKE_FORM = "ZZ Doors Intake"
	PARTNER_USER = "zz-doors-partner@example.com"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge_leads()
		partner_fixture.mint_grain()
		partner_fixture.mint_partner(cls.PARTNER_USER)  # empty grid = the whole catalog, like the FB contract

		cls.fb_contract = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": cls.SOURCE, "enabled": 1,
			"is_internal": 0, "vertical": partner_fixture.VERTICAL, "crm_group": partner_fixture.GROUP,
		}).insert(ignore_permissions=True).name

		frappe.get_doc({
			"doctype": "Facebook Page", "id": cls.PAGE, "page_name": "ZZ Doors Page",
			"category": "Test", "access_token": "zz-token", "account_id": "zz-account",
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Facebook Lead Form", "id": cls.FB_FORM, "page": cls.PAGE,
			"form_name": "ZZ Doors Form",
			"questions": [
				{"key": "q_phone", "label": "Phone", "mapped_to_crm_field": IDENTITY_KEY},
				{"key": "q_name", "label": "Name", "mapped_to_crm_field": "lead:first_name"},
			],
		}).insert(ignore_permissions=True)
		with patch("tatva_connect.lead_sync.source.fetch_and_store_pages", return_value=[]), \
		     patch("tatva_connect.lead_sync.source.refresh_credential", return_value=None):
			frappe.get_doc({
				"doctype": "Lead Sync Source", "name": cls.SOURCE, "type": "Facebook",
				"access_token": "zz-token", "facebook_lead_form": cls.FB_FORM,
				"api_mapping": cls.fb_contract, "background_sync_frequency": "Daily", "enabled": 0,
			}).insert(ignore_permissions=True)

		cls.cfg = frappe.get_doc({
			"doctype": "CRM Intake Form", "form_name": cls.INTAKE_FORM, "enabled": 1,
			"source": "Enrolment Form",
			"custom_vertical": partner_fixture.VERTICAL, "custom_group": partner_fixture.GROUP,
			"mappings": [
				{"source_field": "phone", "fieldtype": "Phone", "target_table": "lead", "target_field": "mobile_no"},
				{"source_field": "patient_name", "target_table": "lead", "target_field": "first_name"},
			],
		}).insert(ignore_permissions=True)
		frappe.clear_cache(doctype="CRM Intake Form")
		cls.intake_doctype, _wf = builder.sync_form(frappe.get_cached_doc("CRM Intake Form", cls.cfg.name))
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge_leads()
		for dt, name in (
			("CRM Intake Form", cls.cfg.name),
			("Lead Sync Source", cls.SOURCE),
			("Facebook Lead Form", cls.FB_FORM),
			("Facebook Page", cls.PAGE),
			("CRM Lead API Mapping", cls.fb_contract),
		):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		intake.bust_intake_doctype_cache()
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge_leads(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610004%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self._switch_was = frappe.db.get_value("CRM Tatva Automation", INTAKE_SWITCH, "enabled")
		if self._switch_was is not None:
			frappe.db.set_value("CRM Tatva Automation", INTAKE_SWITCH, "enabled", 1)
		self.sp = f"doors_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=self.sp)
		if self._switch_was is not None:
			frappe.db.set_value("CRM Tatva Automation", INTAKE_SWITCH, "enabled", self._switch_was)

	# -- the three doors, each driven by its OWN entry point -------------------

	def _through_the_partner_api(self):
		frappe.set_user(self.PARTNER_USER)
		try:
			frappe.form_dict = frappe._dict({"mobile_no": PHONE_PARTNER, "first_name": PATIENT})
			_u, mp, is_sysmgr, parent_fields, child_allow = partner._caller_fields()
			doc, _action = partner._upsert_one(
				frappe.form_dict, mp, is_sysmgr, parent_fields, child_allow, []
			)
		finally:
			frappe.set_user("Administrator")
		return frappe.get_doc("CRM Lead", doc.name)

	def _through_the_intake_form(self):
		frappe.get_doc({
			"doctype": self.intake_doctype, "intake_form": self.cfg.name,
			"phone": PHONE_INTAKE, "patient_name": PATIENT,
		}).insert(ignore_permissions=True)
		name = frappe.db.get_value("CRM Lead", {"mobile_no": PHONE_INTAKE}, "name")
		self.assertIsNotNone(name, "the intake submission did not produce a lead")
		return frappe.get_doc("CRM Lead", name)

	def _through_facebook(self):
		payload = {
			"id": "fb-doors-1",
			"created_time": "2026-07-20T10:00:00+0530",
			"field_data": [
				{"name": "q_phone", "values": [PHONE_FACEBOOK]},
				{"name": "q_name", "values": [PATIENT]},
			],
		}
		fold = TatvaFacebookSyncSource("zz-token", self.FB_FORM, source_name=self.SOURCE)
		doc = fold.sync_single_lead(payload, raise_exception=True)
		self.assertIsNotNone(doc, "the Facebook fold did not produce a lead")
		return frappe.get_doc("CRM Lead", doc.name)

	# -- the comparison --------------------------------------------------------

	@staticmethod
	def _scalar_shape(lead):
		"""Every scalar field of the lead, minus what is allowed to differ. Child tables are compared
		separately, because a list of rows needs its own reading."""
		return {
			k: v for k, v in lead.as_dict().items()
			if not isinstance(v, list) and k not in _MAY_DIFFER
		}

	@staticmethod
	def _filled_child_tables(lead):
		return {k for k, v in lead.as_dict().items() if isinstance(v, list) and v}

	def test_the_three_doors_produce_the_same_lead(self):
		by_door = {
			"partner": self._through_the_partner_api(),
			"intake": self._through_the_intake_form(),
			"facebook": self._through_facebook(),
		}
		shapes = {door: self._scalar_shape(lead) for door, lead in by_door.items()}
		self.assertEqual(
			shapes["intake"], shapes["partner"],
			"an intake lead and a partner lead must be indistinguishable but for the door that made them",
		)
		self.assertEqual(
			shapes["facebook"], shapes["partner"],
			"a Facebook lead and a partner lead must be indistinguishable but for the door that made them",
		)

	def test_every_door_stamps_the_same_grain(self):
		"""The grain is forced from the contract on every door — a door may never take it from a payload."""
		for door, lead in (
			("partner", self._through_the_partner_api()),
			("intake", self._through_the_intake_form()),
			("facebook", self._through_facebook()),
		):
			self.assertEqual(lead.custom_vertical, partner_fixture.VERTICAL, f"{door}: wrong vertical")
			self.assertEqual(lead.custom_group, partner_fixture.GROUP, f"{door}: wrong group")

	def test_each_door_still_records_which_door_it_was(self):
		"""The two permitted differences must actually BE different — otherwise the test above passes by
		the doors having stopped recording their own provenance."""
		sources = {
			self._through_the_partner_api().source,
			self._through_the_intake_form().source,
			self._through_facebook().source,
		}
		self.assertEqual(len(sources), 3, "each door must record itself as its own source")

	def test_no_door_invents_a_child_table_the_others_do_not(self):
		"""None of these payloads carries child data, so a door that filled a child table is defaulting
		something on its own — which is how the doors drifted apart in the first place."""
		filled = {
			"partner": self._filled_child_tables(self._through_the_partner_api()),
			"intake": self._filled_child_tables(self._through_the_intake_form()),
			"facebook": self._filled_child_tables(self._through_facebook()),
		}
		self.assertEqual(filled["intake"], filled["partner"], "intake filled a table the partner API did not")
		self.assertEqual(filled["facebook"], filled["partner"], "Facebook filled a table the partner API did not")
