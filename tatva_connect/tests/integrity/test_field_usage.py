# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A field stored config still names cannot be removed: one planted reference per source must block, and a consumer the field still reaches must not."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access import entitlement
from tatva_connect.integrity import field_usage
from tatva_connect.intake.intake import INTAKE_SWITCH
from tatva_connect.lead_sync.form import IDENTITY_KEY
from tatva_connect.tests.activity import task_type_fixture as ttf
from tatva_connect.tests.api import partner_fixture as pf
from tatva_connect.tests.lead_sync import ensure_app
from tatva_connect.tests.workflow_engine.fixtures import set_switch
from tatva_connect.workflow_engine import ENGINE_SWITCH, versions
from tatva_connect.workflow_engine.tests import fixtures as fx

TASK_FIELD = "zz_guard_score"
SCHEMA = [{"fieldname": TASK_FIELD, "label": "Guard Score", "fieldtype": "Data"}]
PAGE, FORM, SOURCE = "zz-guard-page", "zz-guard-form", "zz-guard-src"

# Every source in `field_usage.SOURCES` and the test that plants it — a new source without a case goes red.
PLANTED = {
	"_workflows": "test_a_workflow_predicate_blocks_the_task_field",
	"_smart_views": "test_a_smart_view_column_blocks_the_task_field",
	"_filter_presets": "test_a_named_smart_view_preset_blocks_the_task_field",
	"_facebook_forms": "test_a_facebook_mapping_blocks_the_source_contract",
	"_intake_forms": "test_an_intake_mapping_blocks_the_internal_contract",
	"_lead_imports": "test_an_unrun_import_blocks_the_source_contract",
}


class TestFieldUsageGuard(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		# Registered before super(): it runs after the class rollback, which fires no hook to drop the cached switch rows.
		cls.addClassCleanup(frappe.clear_document_cache, "CRM Tatva Automation")
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = ttf.mint_type("ZZ Guard A", SCHEMA)
		pf.mint_grain()
		cls.key = pf.mint_catalog_row("zz_guard_lead")
		cls.keep = pf.mint_catalog_row("zz_guard_keep")
		# After the last commit above, so the class rollback restores them: off, a lead starts no journey and an intake form scaffolds no doctype.
		set_switch(ENGINE_SWITCH, 0)
		set_switch(INTAKE_SWITCH, 0)
		cls.internal = cls._contract("zz-guard-internal", is_internal=1)
		cls.source_contract = cls._contract(SOURCE)
		cls._facebook_source()
		setattr(frappe.local, entitlement._INTERNAL_TICKS_CACHE, None)

	@classmethod
	def tearDownClass(cls):
		# The rollback undoes everything after the last commit; the DDL and `ttf.mint_type` committed the rest, which goes through the document API.
		frappe.db.rollback()
		pf.teardown()
		ttf.teardown()
		super().tearDownClass()

	def setUp(self):
		frappe.db.savepoint("field_usage")

	def tearDown(self):
		frappe.db.rollback(save_point="field_usage")

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

	def test_a_named_smart_view_preset_blocks_the_task_field(self):
		view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": "ZZ Guard Preset View", "base_object": "Activity",
			"activity_type": self.task_type, "columns": frappe.as_json([f"activity:{TASK_FIELD}"]),
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "CRM Filter Preset", "label": "ZZ Guard Preset", "user": "Administrator", "is_current": 0,
			"reference_doctype": "CRM Smart View", "reference_name": view.name,
			"filters": frappe.as_json({f"activity:{TASK_FIELD}": "x"}),
		}).insert(ignore_permissions=True)
		self.assertRefused(self._drop_task_field, "CRM Filter Preset")

	def test_a_facebook_mapping_blocks_the_source_contract(self):
		form = frappe.get_doc("Facebook Lead Form", FORM)
		for question in form.questions:
			question.mapped_to_crm_field = IDENTITY_KEY if question.key == "q_phone" else self.key
		form.save(ignore_permissions=True)
		self.assertRefused(lambda: self._drop_tick(self.source_contract), "Facebook Lead Form")

	def test_an_intake_mapping_blocks_the_internal_contract(self):
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

	def test_an_older_version_blocks_only_while_a_journey_is_live_on_it(self):
		"""A superseded version naming the field blocks while a journey is live on it, and stops once none is."""
		workflow = self._task_workflow()
		older = versions.ensure_version(workflow)
		trigger = frappe.get_doc("CRM Workflow Node", {"workflow": workflow.name, "node_id": "start"})
		config = frappe.parse_json(trigger.config_json)
		config.pop("predicate")
		trigger.config_json = frappe.as_json(config)
		trigger.save(ignore_permissions=True)
		versions.ensure_version(workflow.reload())

		journey = frappe.get_doc({
			"doctype": "CRM Workflow Journey", "workflow": workflow.name, "workflow_version": older,
			"subject_doctype": "CRM Lead", "subject_name": fx.make_lead().name, "current_node": "end", "status": "Parked",
		}).insert(ignore_permissions=True)
		self.assertRefused(self._drop_task_field, "CRM Workflow")

		journey.status = "Done"
		journey.save(ignore_permissions=True)
		self._drop_task_field()

	def test_a_task_type_a_workflow_creates_cannot_be_deleted_or_renamed(self):
		"""A Create Task node stores the type's docname, so deleting or renaming the type is refused."""
		fx.make_workflow(f"ZZ-GUARD-{frappe.generate_hash(length=6)}", [
			fx.trigger(to="raise", grain=False),
			fx.node("raise", "Create Task", edges={"next": "end"}, config={"task_type": self.task_type}),
			fx.node("end", "Terminal"),
		], lifecycle_state="Draft")
		self.assertRefused(lambda: frappe.delete_doc("CRM Task Type", self.task_type, ignore_permissions=True), "CRM Workflow")
		self.assertRefused(lambda: frappe.rename_doc("CRM Task Type", self.task_type, f"{self.task_type} Renamed"), "CRM Workflow")

	def test_every_master_a_node_names_is_guarded_on_delete_and_rename(self):
		"""Every Link target a node declares carries `guard_record` on both events in `hooks.py`."""
		from tatva_connect import hooks

		guard = "tatva_connect.integrity.field_usage.guard_record"
		for doctype in field_usage.guarded_doctypes():
			with self.subTest(doctype=doctype):
				events = hooks.doc_events.get(doctype, {})
				self.assertEqual((events.get("on_trash"), events.get("before_rename")), (guard, guard))

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
