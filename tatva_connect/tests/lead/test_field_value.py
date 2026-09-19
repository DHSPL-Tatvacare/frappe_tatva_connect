# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`lead.field_value` names where a catalog field's value lives, and every kind is proved against the live schema.

The defect it replaces: a multi-value field whose section still carried a dead column of the same name
read as that column — blank, in silence — and a multi-value field with no column answered `1054`. A
virtual field answered `1054` in SQL and None through `doc.get`. Each test below is RED on the rule it
guards: the kind is asked, never inferred from whether a column happens to exist.

Data-driven: fields are found off the declaration and the schema, never named; a site without one skips.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_field_value
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_years, cint, nowdate

from tatva_connect.lead import field_value, multi_value

LEAD = "CRM Lead"


def _section(name):
	return frappe.get_cached_doc("CRM Lead Section", name)


def _lead_section():
	"""The section whose target is the lead itself — read off the brain, never named."""
	name = frappe.db.get_value("CRM Lead Section", {"target_doctype": LEAD, "child_table_field": ("is", "not set")})
	return _section(name) if name else None


def _virtual_lead_field():
	"""A virtual DocField on CRM Lead, or None."""
	return next((df for df in frappe.get_meta(LEAD).fields if cint(df.get("is_virtual"))), None)


class TestFieldValueKinds(FrappeTestCase):
	def test_standard_fields_are_columns_and_virtual_and_table_fields_are_not(self):
		self.assertTrue(field_value.is_column(LEAD, "name"))
		self.assertTrue(field_value.is_column(LEAD, "creation"))
		for df in frappe.get_meta(LEAD).fields:
			if cint(df.get("is_virtual")) or df.fieldtype in ("Table", "Table MultiSelect"):
				self.assertFalse(field_value.is_column(LEAD, df.fieldname), df.fieldname)

	def test_a_multi_value_field_is_its_selections_even_where_a_dead_column_shares_its_name(self):
		declared = multi_value.declared()
		if not declared:
			self.skipTest("no multi-value field is declared")
		for (section_key, fieldname), field_key in declared.items():
			row = frappe._dict(field_key=field_key, fieldname=fieldname, is_multi_value=1)
			self.assertEqual(field_value.kind_of(_section(section_key), row), field_value.MULTI_VALUE, field_key)
			self.assertFalse(field_value.in_sql(field_value.MULTI_VALUE))
			self.assertTrue(field_value.on_page(field_value.MULTI_VALUE))
			self.assertEqual(field_value.docfield(_section(section_key), row).options,
			                 multi_value.value_field().options)

	def test_a_real_column_keeps_its_sections_own_word(self):
		section = _lead_section()
		if not section:
			self.skipTest("no section targets the lead itself")
		row = frappe._dict(fieldname="mobile_no")
		self.assertEqual(field_value.kind_of(section, row), field_value.PARENT)
		self.assertTrue(field_value.in_sql(field_value.kind_of(section, row)))

	def test_a_virtual_field_is_never_sql_and_reads_through_frappes_own_computation(self):
		section, df = _lead_section(), _virtual_lead_field()
		if not (section and df):
			self.skipTest("no virtual field on the lead")
		row = frappe._dict(fieldname=df.fieldname)
		self.assertEqual(field_value.kind_of(section, row), field_value.VIRTUAL)
		self.assertFalse(field_value.on_page(field_value.VIRTUAL))
		# `doc.get` answers None for a virtual field; `read` must answer what frappe computes.
		doc = frappe.new_doc(LEAD)
		doc.custom_dob = add_years(nowdate(), -40)
		self.assertIsNone(doc.get(df.fieldname))
		self.assertEqual(field_value.read(doc, section, row), doc.get_valid_dict().get(df.fieldname))

	def test_a_field_that_resolves_to_nothing_has_no_kind(self):
		section = _lead_section()
		if not section:
			self.skipTest("no section targets the lead itself")
		self.assertIsNone(field_value.kind_of(section, frappe._dict(fieldname="zz_not_a_field_anywhere")))
