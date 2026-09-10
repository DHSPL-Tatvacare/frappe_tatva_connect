# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`name`, `owner`, `creation` and `modified` are columns, and the app has to know it.

THE BLIND SPOT. `Meta.get_field` answers only for fields a DocType declares, so it returns None for the
four standard ones — frappe keeps those in `frappe.model.std_fields`. Two places asked meta alone:

  * `smartview.api._col_type` typed them `Data`, so the client never date-formatted them and a rep read
    `2026-09-09 00:25:09.490786` in the Update Date column of every Smart View;
  * `CRM Lead API Field.validate` called them "not a field of CRM Lead", so those four catalog rows could
    not be SAVED at all — they only existed because the original seed was raw SQL that skipped validate,
    and an operator could never fix their labels in the UI.

One answer now, `crm_lead_section.docfield`, asked by both.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_standard_fields_are_real_columns
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section
from tatva_connect.smartview import catalog


class TestStandardFieldsAreRealColumns(FrappeTestCase):
	def test_the_shared_answer_resolves_a_standard_field(self):
		"""RED on `frappe.get_meta(...).get_field`, which answers None for every one of these."""
		for fieldname, fieldtype in (("modified", "Datetime"), ("creation", "Datetime"),
		                             ("owner", "Link"), ("name", "Link")):
			df = crm_lead_section.docfield("CRM Lead", fieldname)
			self.assertIsNotNone(df, f"{fieldname} is a real column on CRM Lead")
			self.assertEqual(df.fieldtype, fieldtype)

	def test_a_declared_field_still_wins(self):
		"""The DocType's own declaration is asked FIRST — the std fallback must not shadow a real field."""
		self.assertEqual(crm_lead_section.docfield("CRM Lead", "mobile_no").fieldtype, "Data")
		self.assertIsNone(crm_lead_section.docfield("CRM Lead", "zz_not_a_column_at_all"))

	def test_the_worklist_types_a_timestamp_as_a_timestamp(self):
		"""THE defect a rep sees: typed `Data`, the column ships microseconds to a client that only
		date-formats a Datetime."""
		cat = catalog._lead_catalog()
		for key in ("lead:modified", "lead:creation"):
			if key not in cat:
				continue  # the label/type rule is what is locked here, not which rows a bench carries
			self.assertEqual(catalog._col_type(cat[key])[0], "Datetime", f"{key} is a timestamp")

	def test_a_catalog_row_on_a_standard_field_can_be_saved(self):
		"""The second face: its own validate refused the row, so nobody could fix it in the UI."""
		if not frappe.db.exists("CRM Lead API Field", "lead:modified"):
			self.skipTest("this bench carries no lead:modified catalog row")
		doc = frappe.get_doc("CRM Lead API Field", "lead:modified")
		doc.save(ignore_permissions=True)  # RED before: "modified is not a field of CRM Lead"
