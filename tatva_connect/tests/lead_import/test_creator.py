# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The import writes through the SAME closure the partner API and the async worker use.

Two things are pinned here. First, the grain comes from the import's contract and not from the session
user: a Desk operator is a System Manager, and the partner path would hand that caller `is_sysmgr=True`
with no mapping, which admits routing off the payload — a spreadsheet column called `custom_vertical`
would then choose its own business line. Second, the dry run writes nothing: it runs the live closure
inside a savepoint and rolls back, so a validated file has left no lead behind.

Placement is not tested here because this module does none — `contract.stage()` owns it, and its own
tests cover the four section shapes.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api._base import _process_bulk
from tatva_connect.lead_import import creator as import_creator
from tatva_connect.tests.api import partner_fixture

PARTNER = "lead.import.creator@example.test"
SECTION = partner_fixture.PARENT_SECTION
PHONE_PREFIX = "+91610777"  # a distinctive range this module owns outright, purged in tearDownClass


def _purge_leads():
	for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"{PHONE_PREFIX}%"]}, pluck="name"):
		frappe.delete_doc("CRM Lead", name, force=True,
		                  ignore_permissions=True)  # authz-ok: tier-a — test fixture teardown


class TestLeadImportCreator(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		partner_fixture.mint_partner(PARTNER)  # empty tick grid -> the whole catalogue
		cls.contract_name = frappe.get_value("CRM Lead API Mapping", {"partner_user": PARTNER}, "name")
		cls.contract = frappe.get_doc("CRM Lead API Mapping", cls.contract_name)
		_purge_leads()
		frappe.db.commit()  # survives the per-test rollback; the closures resolve the contract live

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_purge_leads()
		for name in frappe.get_all("CRM Lead Import", filters={"contract": cls.contract_name}, pluck="name"):
			frappe.delete_doc("CRM Lead Import", name, force=True,
			                  ignore_permissions=True)  # authz-ok: tier-a — test fixture teardown
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	def _import(self):
		doc = frappe.new_doc("CRM Lead Import")
		doc.contract = self.contract_name
		doc.append("columns", {"source_column": "Phone", "target_table": SECTION, "target_field": "mobile_no"})
		doc.append("columns", {"source_column": "Name", "target_table": SECTION, "target_field": "first_name"})
		doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		return doc

	def test_the_grain_is_read_off_the_contract(self):
		grain = import_creator.grain_of(self.contract)
		self.assertEqual((grain.vertical, grain.crm_group, grain.source),
		                 (self.contract.vertical, self.contract.crm_group, self.contract.source))

	def test_a_row_is_staged_by_the_placement_brain_not_by_this_module(self):
		item = import_creator.stage_row({"Phone": f"{PHONE_PREFIX}001", "Name": "Asha"},
		                                {"Phone": f"{SECTION}:mobile_no", "Name": f"{SECTION}:first_name"})
		self.assertEqual(item["mobile_no"], f"{PHONE_PREFIX}001")
		self.assertEqual(item["first_name"], "Asha")

	def test_an_empty_cell_is_not_sent_rather_than_erasing(self):
		item = import_creator.stage_row({"Phone": f"{PHONE_PREFIX}002", "Name": "   "},
		                                {"Phone": f"{SECTION}:mobile_no", "Name": f"{SECTION}:first_name"})
		self.assertNotIn("first_name", item)

	def test_a_routing_column_in_the_sheet_cannot_choose_the_grain(self):
		"""The sheet may carry a vertical column; the contract still wins, because _force_routing stamps it."""
		imp = self._import()
		result = import_creator.live_creator(imp)(0, {"Phone": f"{PHONE_PREFIX}003", "Name": "Bina",
		                                              "custom_vertical": "Not This One"})
		lead = frappe.get_doc("CRM Lead", result["data"]["name"])
		self.assertEqual(lead.custom_vertical, self.contract.vertical)
		self.assertEqual(lead.custom_group, self.contract.crm_group)
		self.assertEqual(lead.source, self.contract.source)

	def test_the_live_closure_reports_the_same_shape_the_worker_records(self):
		imp = self._import()
		result = import_creator.live_creator(imp)(0, {"Phone": f"{PHONE_PREFIX}004", "Name": "Chetan"})
		self.assertEqual(result["status"], "success")
		self.assertIn(result["action"], ("created", "updated"))
		self.assertTrue(result["data"]["name"])

	def test_the_dry_run_leaves_no_lead_behind(self):
		imp = self._import()
		phone = f"{PHONE_PREFIX}005"
		results, summary = _process_bulk([{"Phone": phone, "Name": "Dev"}], import_creator.dry_creator(imp))
		self.assertEqual(summary["succeeded"], 1)
		self.assertEqual(results[0]["action"], "validated")
		self.assertFalse(frappe.db.exists("CRM Lead", {"mobile_no": phone}), "the dry run wrote a lead")

	def test_a_row_the_live_path_would_refuse_fails_validation_with_its_reason(self):
		"""Identity is required; a validation that passed such a row would be lying about the import."""
		imp = self._import()
		results, summary = _process_bulk([{"Phone": "", "Name": "Eshan"}], import_creator.dry_creator(imp))
		self.assertEqual(summary["failed"], 1)
		self.assertEqual(results[0]["status"], "error")

	def test_validating_then_importing_the_same_row_creates_it_exactly_once(self):
		"""The dry run must not consume the row: a validated file still imports in full."""
		imp = self._import()
		phone = f"{PHONE_PREFIX}006"
		row = {"Phone": phone, "Name": "Farah"}
		_process_bulk([row], import_creator.dry_creator(imp))
		self.assertFalse(frappe.db.exists("CRM Lead", {"mobile_no": phone}))
		import_creator.live_creator(imp)(0, row)
		self.assertEqual(frappe.db.count("CRM Lead", {"mobile_no": phone}), 1)
