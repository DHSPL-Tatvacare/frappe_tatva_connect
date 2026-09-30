# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An Activity view offers what the task form holds: the task's own columns (the native Task column lens) and the type's form fields, each stored column once, read as the form reads it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_activity_view_is_the_task_form
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.api import task_lenses
from tatva_connect.smartview import api as smartview
from tatva_connect.smartview import catalog

LABEL = "ZZ Activity Form View"


class TestActivityViewIsTheTaskForm(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = frappe.db.get_value("CRM Task Type", {"enabled": 1, "vertical": ["!=", ""]}, "name")
		if not cls.task_type:
			raise unittest.SkipTest("no activity type on this site")
		tt = frappe.db.get_value("CRM Task Type", cls.task_type, ["vertical", "group", "program"], as_dict=True)
		cls.view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": LABEL, "base_object": "Activity", "activity_type": cls.task_type,
			"is_standard": 1, "vertical": tt.vertical, "group": tt.group, "program": tt.program,
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in frappe.get_all("CRM Smart View", filters={"label": LABEL}, pluck="name"):
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_the_picker_offers_the_task_columns_and_the_form_fields_once(self):
		"""Every native Task column that is a column, every form field, no stored column twice, form fields grouped by the form's own sections."""
		offered = smartview.field_catalog("Activity", activity_type=self.task_type)
		keys = {c["field_key"] for c in offered}
		on_row = [c["fieldname"] for c in offered if c["sql_source"] == catalog.TASK]
		self.assertEqual(len(on_row), len(set(on_row)), "a task column is offered once")
		lens = {c["fieldname"] for c in task_lenses.get_column_fields(catalog.TASK_DOCTYPE)}
		self.assertEqual(lens & set(frappe.get_meta(catalog.TASK_DOCTYPE).get_valid_columns()) - set(on_row), set())
		cfg = activity_api._type_config(self.task_type)
		self.assertEqual({catalog.activity_key(f["fieldname"]) for f in cfg["fields"]} - keys, set())
		containers = {x["label"] for t in cfg["tabs"] for x in (t, *t["sections"])}
		self.assertTrue({c["section_title"] for c in offered if c["field_key"].startswith("activity:") and c["section_title"]} <= containers)
		self.assertEqual({c["field_key"] for c in offered if c["always_shown"]},
		                 {catalog.task_key("reference_docname"), catalog.task_key("creation")})

	def test_the_list_draws_the_lead_as_its_chip(self):
		"""The grid's identity column is the activity's lead, as a Lead view's is the lead ID."""
		columns = smartview.get_data(self.view, page_size=1)["columns"]
		self.assertEqual([c["key"] for c in columns if c["identity"]], [catalog.task_key("reference_docname")])

	def test_a_task_section_reads_the_row_its_writer_fills(self):
		"""A single-row section is read at rows[0], the row `_put_section_value` writes — never the highest row key. RED before: `_latest` sorted by row key."""
		section = next(s for s in activity_api._sections() if s.name == "documents")
		held = {section.child_table_field: [frappe._dict(idx=1, document_kind="A", prescription_summary="first"),
		                                    frappe._dict(idx=2, document_kind="Z", prescription_summary="second")]}
		f = frappe._dict(fieldname="zz_summary", target="prescription_summary", section="documents")
		self.assertEqual(activity_api._section_answer(f, frappe._dict(), held, {section.name: section}), "first")

	def test_a_page_of_one_type_is_an_index_walk(self):
		"""The page `get_data` asks (type pinned, newest first, name tiebreak in the same direction) seeks and walks `ix_task_type_modified`, never a filesort."""
		self.assertTrue(frappe.db.has_index("tabCRM Task", "ix_task_type_modified"), "the index ships with the patch")
		plan = frappe.db.sql(
			"EXPLAIN SELECT name FROM `tabCRM Task` WHERE custom_task_type=%s ORDER BY modified DESC, name DESC LIMIT 50",
			self.task_type, as_dict=True)[0]
		self.assertEqual(plan.key, "ix_task_type_modified")
		self.assertNotIn("filesort", plan.Extra or "")

	def test_every_offered_field_sits_in_a_named_section(self):
		"""The grouped picker reads its FIRST group's name to decide it is grouped; a nameless task group rendered the whole list blank."""
		offered = smartview.field_catalog("Activity", activity_type=self.task_type)
		self.assertTrue(all(c["section_title"] for c in offered if c["field_key"].startswith("task:")))

	def test_a_lead_sourced_field_is_typed_as_its_lead_column(self):
		"""A `source = Lead` form field is the lead's column, so it is typed as the Data tab types it (a picklist reads its label, not its key)."""
		lead_types = {r.fieldname: catalog._col_type(r) for r in catalog._lead_catalog().values()}
		checked = 0
		for tt in frappe.get_all("CRM Task Type", filters={"enabled": 1, "vertical": ["!=", ""]}, pluck="name"):
			cat = catalog._activity_catalog(tt)
			for f in activity_api.get_schema(tt):
				if f.get("source") == activity_api.LEAD_SOURCE and f["fieldname"] in lead_types:
					self.assertEqual(catalog._col_type(cat[catalog.activity_key(f["fieldname"])]), lead_types[f["fieldname"]], f["fieldname"])
					checked += 1
		if not checked:
			self.skipTest("no lead-sourced activity field on this site")
