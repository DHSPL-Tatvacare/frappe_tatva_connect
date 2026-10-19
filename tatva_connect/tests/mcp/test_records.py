# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The record tools — rows of the listed tables only, and never more than Frappe would hand the same login.

Frappe is the oracle (S.6): what a rep reads through the tool is a subset of what `frappe.get_list` gives
that rep. The list is set through the document API, as an operator's save sets it, and rolled back.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.mcp.test_records
"""
import frappe
from frappe.model import table_fields
from frappe.tests import IntegrationTestCase

from tatva_connect.mcp import ToolError, records, schema, settings

REP = "zz-mcp-records-rep@example.com"


class TestRecordTools(IntegrationTestCase):
	def setUp(self):
		self._list("CRM Lead Status", "User")
		self.addCleanup(frappe.clear_document_cache, settings.SETTINGS, settings.SETTINGS)
		self.addCleanup(frappe.set_user, "Administrator")

	def _list(self, *doctypes):
		row = frappe.get_single(settings.SETTINGS)
		row.set(records.READABLE_FIELD, [{"document_type": doctype} for doctype in doctypes])
		row.save()

	def test_a_table_not_listed_is_never_read_and_an_empty_list_reads_nothing(self):
		for doctype in ("CRM Lead", "CRM Task", "CRM Call Log", "File", "Version"):
			with self.assertRaises(ToolError, msg=doctype):
				records.search_records({"doctype": doctype})
			with self.assertRaises(ToolError, msg=doctype):
				records.get_record({"doctype": doctype, "name": "anything"})
		self._list()
		self.assertEqual(records.list_tables({})["tables"], [])

	def test_a_listed_framework_table_is_described_and_an_unlisted_one_is_not(self):
		self.assertEqual(schema.get_schema({"doctype": "User"})["doctype"], "User")
		with self.assertRaises(ToolError):
			schema.get_schema({"doctype": "Role"})

	def test_the_form_refuses_a_repeat_or_a_child_table(self):
		for doctypes in (("CRM Lead Status", "CRM Lead Status"), ("CRM Task Type Field",)):
			with self.assertRaises(frappe.ValidationError, msg=doctypes):
				self._list(*doctypes)

	def test_a_rep_reads_no_more_than_frappe_gives_the_same_login(self):
		frappe.get_doc({"doctype": "User", "email": REP, "first_name": "ZZ MCP Rep", "send_welcome_email": 0,
		                "roles": [{"role": "Sales User"}]}).insert()
		frappe.set_user(REP)
		native = set(frappe.get_list("CRM Lead Status", pluck="name", limit_page_length=0))
		read = {row["name"] for row in records.search_records({"doctype": "CRM Lead Status"})["rows"]}
		self.assertTrue(read, "the rep can read lead statuses in Desk, so the tool must too")
		self.assertLessEqual(read, native)

	def test_a_record_is_what_its_form_shows_never_a_password_or_hidden_field(self):
		record = records.get_record({"doctype": "User", "name": "Administrator"})
		withheld = {df.fieldname for df in frappe.get_meta("User").fields
		            if df.fieldtype == "Password" or (df.hidden and df.fieldtype not in table_fields)}
		self.assertTrue(withheld, "User must carry Password and hidden fields for this proof to mean anything")
		self.assertFalse(withheld & set(record), f"sent: {withheld & set(record)}")
		self.assertIn("roles", record)
		self.assertTrue(record["urls"]["form"].endswith("/desk/user/Administrator"))
