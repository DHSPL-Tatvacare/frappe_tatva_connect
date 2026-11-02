# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A completed task prints as its form, page per tab, under the Letter Head its grain picks.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.activity.test_task_print
"""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.activity import task_print
from tatva_connect.taxonomy.doctype.crm_task_type import crm_task_type as task_forms
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

GRAIN = next(g for g in GRAINS if g["key"] == "Goodflip-Care::Anaya::Nivolumab")
SCHEMA = [
	{"label": "ZZ Visit", "fieldname": "zz_tp_visit_tab", "fieldtype": "Tab Break"},
	{"label": "ZZ Done", "fieldname": "zz_tp_done", "fieldtype": "Select", "options": "Yes\nNo"},
	{"label": "ZZ Why Not", "fieldname": "zz_tp_why", "fieldtype": "Data", "depends_on": "eval:doc.zz_tp_done=='No'"},
	{"label": "ZZ Notes", "fieldname": "zz_tp_section", "fieldtype": "Section Break"},
	{"label": "ZZ Remark", "fieldname": "zz_tp_remark", "fieldtype": "Small Text"},
	{"label": "ZZ Device", "fieldname": "zz_tp_device_tab", "fieldtype": "Tab Break"},
	{"label": "ZZ Device Kind", "fieldname": "zz_tp_device", "fieldtype": "Data"},
]


class TestTaskPrint(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		assert_masters_exist()
		cls.form = frappe.get_doc({
			"doctype": "CRM Task Type", "type_name": "ZZ Task Print Probe", "is_logged_complete": 1,
			"vertical": GRAIN["vertical"], "group": GRAIN["group"], "program": GRAIN["program"], "schema": SCHEMA,
		}).insert(ignore_permissions=True).name
		task_forms.publish(cls.form)
		task_forms.activate(cls.form)
		cls.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ Task Print Probe",
			"mobile_no": f"+91900000{int(frappe.generate_hash(length=8), 16) % 10000:04d}",
			"custom_vertical": GRAIN["vertical"], "custom_group": GRAIN["group"], "custom_current_program": GRAIN["program"],
		}).insert(ignore_permissions=True).name
		cls.task = activity_api.save_activity(cls.lead, cls.form, {
			"zz_tp_done": "Yes", "zz_tp_remark": "ZZ line one\nZZ line two", "zz_tp_device": "Laptop",
		})

	def test_a_completed_task_previews_as_its_form(self):
		html = task_print.preview(self.task)["html"]
		for shown in ("ZZ Visit", "ZZ Device", "Laptop", "page-break", "ZZ line one\nZZ line two"):
			self.assertIn(shown, html)
		self.assertNotIn("ZZ Why Not", html, "a field the form hides is not printed")
		self.assertNotIn("&lt;br&gt;", html, "a multi-line answer keeps its line breaks, not an escaped tag")

	def test_an_open_task_does_not_print(self):
		todo = frappe.get_doc({
			"doctype": "CRM Task", "title": "ZZ Task Print Open", "status": "Todo", "custom_task_type": self.form,
			"reference_doctype": "CRM Lead", "reference_docname": self.lead,
		}).insert(ignore_permissions=True).name
		with self.assertRaises(frappe.ValidationError):
			task_print.preview(todo)

	def test_the_most_specific_letter_head_wins(self):
		def letter_head(name, **axes):
			return frappe.get_doc({"doctype": "Letter Head", "letter_head_name": name, "source": "HTML",
								   "content": "<p>x</p>", **axes}).insert(ignore_permissions=True).name

		letter_head("ZZ TP Vertical", grain_vertical=GRAIN["vertical"])
		program = letter_head("ZZ TP Program", grain_vertical=GRAIN["vertical"], grain_program=GRAIN["program"])
		letter_head("ZZ TP Sitewide")
		self.assertEqual(task_print.preview(self.task)["letterhead"], program)
