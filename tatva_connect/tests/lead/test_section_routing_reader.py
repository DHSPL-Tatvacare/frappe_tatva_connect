# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Section routing is STRUCTURAL — read off the native CRM Lead Section Document, not a copied map.

The Data tab no longer keeps a private SECTION_REGISTRY / child_pick string. A section's table,
target, multi-row-ness and row key live once on `CRM Lead Section`; this test pins that a reader
gets them straight off the doc's fields.
"""
import frappe
from frappe.tests.utils import FrappeTestCase


class TestSectionRoutingIsStructural(FrappeTestCase):
	def test_lab_routing_reads_off_the_section_doc(self):
		sec = frappe.get_cached_doc("CRM Lead Section", "lab")
		self.assertEqual(sec.target_doctype, "CRM Lab Profile")
		self.assertEqual(sec.child_table_field, "custom_lab_profile")
		self.assertTrue(sec.is_multi_row)
		self.assertEqual(sec.row_key_field, "report_date")
