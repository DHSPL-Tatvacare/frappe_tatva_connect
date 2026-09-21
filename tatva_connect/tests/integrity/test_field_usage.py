# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A field stored config still names cannot be removed: one planted reference per source must block, and a consumer the field still reaches must not.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.integrity.test_field_usage
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.integrity import field_usage
from tatva_connect.lead_sync.form import IDENTITY_KEY
from tatva_connect.tests.activity import task_type_fixture as ttf
from tatva_connect.tests.api import partner_fixture as pf
from tatva_connect.tests.lead_sync import ensure_app
from tatva_connect.workflow_engine.tests import fixtures as fx

TASK_FIELD = "zz_guard_score"
SCHEMA = [{"fieldname": TASK_FIELD, "label": "Guard Score", "fieldtype": "Data"}]
PAGE, FORM, SOURCE = "zz-guard-page", "zz-guard-form", "zz-guard-src"

# Every source in `field_usage.SOURCES` and the test that plants it — a new source without a case goes red.
PLANTED = {
	"_workflows": "test_a_workflow_predicate_blocks_the_task_field",
	"_smart_views": "test_a_smart_view_column_blocks_the_task_field",
	"_facebook_forms": "test_a_facebook_mapping_blocks_the_source_contract",
	"_intake_forms": "test_an_intake_mapping_blocks_the_internal_contract",
	"_lead_imports": "test_an_unrun_import_blocks_the_source_contract",
}


class TestFieldUsageGuard(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = ttf.mint_type("ZZ Guard A", SCHEMA)
		pf.mint_grain()
		cls.key = pf.mint_catalog_row("zz_guard_lead")
		cls.keep = pf.mint_catalog_row("zz_guard_keep")
		cls.internal = cls._contract("zz-guard-internal", is_internal=1)
		cls.source_contract = cls._contract(SOURCE)
		cls._facebook_source()
		setattr(frappe.local, entitlement._INTERNAL_TICKS_CACHE, None)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()
		for doctype, name in (("Lead Sync Source", SOURCE), ("Facebook Lead Form", FORM), ("Facebook Page", PAGE),
		                      ("CRM Lead API Mapping", cls.internal), ("CRM Lead API Mapping", cls.source_contract)):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		ttf.teardown()
		pf.teardown()
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		frappe.db.rollback()

	@classmethod
	def _contract(cls, name, is_internal=0):
		return frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": name, "enabled": 1, "is_internal": is_internal,
			"vertical": pf.VERTICAL, "crm_group": pf.GROUP,
			"allowed_fields": [{"field": cls.key}, {"field": cls.keep}],
		}).insert(ignore_permissions=True).name

	@classmethod
	def _facebook_source(cls):
		frappe.get_doc({
			"doctype": "Facebook Page", "id": PAGE, "page_name": "ZZ Guard Page",
			"category": "Test", "access_token": "zz-token", "account_id": "zz-account",
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Facebook Lead Form", "id": FORM, "page": PAGE, "form_name": "ZZ Guard Form",
			"questions": [{"key": "q_phone", "label": "Phone"}, {"key": "q_other", "label": "Other"}],
		}).insert(ignore_permissions=True)
		with patch("tatva_connect.lead_sync.source.fetch_and_store_pages", return_value=[]), \
			patch("tatva_connect.lead_sync.source.refresh_credential", return_value=None):
			frappe.get_doc({
				"doctype": "Lead Sync Source", "facebook_app": ensure_app(), "name": SOURCE, "type": "Facebook",
				"access_token": "zz-token", "facebook_lead_form": FORM, "api_mapping": cls.source_contract,
				"background_sync_frequency": "Daily", "enabled": 0,
			}).insert(ignore_permissions=True)

	def _task_workflow(self, vertical=ttf.VERTICAL, group=ttf.GROUP):
		trigger = fx.node("start", "Trigger", edges={"next": "end"}, config={
			"subject_doctype": "CRM Task", "event": "Created", "vertical": vertical, "group": group,
			"predicate": {"type": "rule", "field": f"crm_task.{TASK_FIELD}", "operator": "is", "value": "1"},
		})
		return fx.make_workflow(f"ZZ-GUARD-{frappe.generate_hash(length=6)}", [trigger, fx.node("end", "Terminal")], lifecycle_state="Draft")

	def _drop_task_field(self):
		doc = frappe.get_doc("CRM Task Type", self.task_type)
		doc.set("schema", [row for row in doc.schema if row.fieldname != TASK_FIELD])
		doc.save(ignore_permissions=True)

	def _drop_tick(self, contract):
		doc = frappe.get_doc("CRM Lead API Mapping", contract)
		doc.set("allowed_fields", [row for row in doc.allowed_fields if row.field != self.key])
		doc.save(ignore_permissions=True)

	def assertRefused(self, remove, doctype):
		with self.assertRaises(frappe.LinkExistsError) as caught:
			remove()
		self.assertIn(doctype, str(caught.exception))

	def test_a_workflow_predicate_blocks_the_task_field(self):
		self._task_workflow()
		self.assertRefused(self._drop_task_field, "CRM Workflow")

	def test_a_workflow_write_blocks_the_catalog_field(self):
		fx.make_workflow(f"ZZ-GUARD-{frappe.generate_hash(length=6)}", [
			fx.trigger(to="mark", grain=False),
			fx.node("mark", "Update Field", edges={"next": "end"}, config={
				"target_doctype": "CRM Lead", "updates": [{"name": "zz_guard_lead", "mode": "Literal", "value": "x"}],
			}),
			fx.node("end", "Terminal"),
		], lifecycle_state="Draft")
		self.assertRefused(lambda: frappe.delete_doc("CRM Lead API Field", self.key, ignore_permissions=True), "CRM Workflow")

	def test_a_smart_view_column_blocks_the_task_field(self):
		frappe.get_doc({
			"doctype": "CRM Smart View", "label": "ZZ Guard View", "base_object": "Activity",
			"activity_type": self.task_type, "columns": frappe.as_json([f"activity:{TASK_FIELD}"]),
		}).insert(ignore_permissions=True)
		self.assertRefused(self._drop_task_field, "CRM Smart View")

	def test_a_facebook_mapping_blocks_the_source_contract(self):
		form = frappe.get_doc("Facebook Lead Form", FORM)
		for question in form.questions:
			question.mapped_to_crm_field = IDENTITY_KEY if question.key == "q_phone" else self.key
		form.save(ignore_permissions=True)
		self.assertRefused(lambda: self._drop_tick(self.source_contract), "Facebook Lead Form")

	def test_an_intake_mapping_blocks_the_internal_contract(self):
		with patch("tatva_connect.intake.builder.sync_form"):
			frappe.get_doc({
				"doctype": "CRM Intake Form", "form_name": "ZZ Guard Intake", "enabled": 0,
				"custom_vertical": pf.VERTICAL, "custom_group": pf.GROUP,
				"mappings": [{"source_field": "zz_answer", "target_table": "lead", "target_field": "zz_guard_lead"}],
			}).insert(ignore_permissions=True)
		self.assertRefused(lambda: self._drop_tick(self.internal), "CRM Intake Form")

	def test_an_unrun_import_blocks_the_source_contract(self):
		frappe.get_doc({
			"doctype": "CRM Lead Import", "contract": self.source_contract,
			"columns": [{"source_column": "Answer", "target_table": "lead", "target_field": "zz_guard_lead"}],
		}).insert(ignore_permissions=True)
		self.assertRefused(lambda: self._drop_tick(self.source_contract), "CRM Lead Import")

	def test_a_field_another_type_still_declares_is_not_blocked(self):
		self._task_workflow()
		frappe.get_doc({
			"doctype": "CRM Task Type", "type_name": "ZZ Guard B", "vertical": ttf.VERTICAL, "group": ttf.GROUP,
			"schema": SCHEMA,
		}).insert(ignore_permissions=True)
		self._drop_task_field()

	def test_a_workflow_on_another_grain_is_not_blocked(self):
		frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": "ZZ Guard Far Line"}).insert(ignore_permissions=True)
		self._task_workflow(vertical="ZZ Guard Far Line", group="")
		self._drop_task_field()

	def test_every_source_has_a_planted_case(self):
		self.assertEqual({source.__name__ for source in field_usage.SOURCES}, set(PLANTED))
		for name in PLANTED.values():
			self.assertTrue(hasattr(self, name), name)
