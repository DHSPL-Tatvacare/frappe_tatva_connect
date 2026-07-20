# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The import document itself: its grain is clamped, its mapping is backstopped, its gate cannot be skipped.

The column check asks the SAME seam that fed the picker (`lead/mapping.py`), so a mapping saved by a
client that never opened the picker — or opened it before the contract's ticks moved — is refused on the
way in rather than discovered mid-import. That is the whole point of a backstop existing at all.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.api import partner_fixture

PARTNER = "lead.import.partner@example.test"
SECTION = partner_fixture.PARENT_SECTION


class TestCRMLeadImport(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.allowed_key = partner_fixture.mint_catalog_row("zz_import_allowed")
		cls.denied_key = partner_fixture.mint_catalog_row("zz_import_denied")
		partner_fixture.mint_partner(PARTNER, ticks=(cls.allowed_key,))
		cls.contract = frappe.get_value("CRM Lead API Mapping", {"partner_user": PARTNER}, "name")
		frappe.db.commit()  # survives the per-test rollback; validate resolves the contract live

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in frappe.get_all("CRM Lead Import", filters={"contract": cls.contract}, pluck="name"):
			frappe.delete_doc("CRM Lead Import", name, force=True,
			                  ignore_permissions=True)  # authz-ok: tier-a — test fixture teardown
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	def _doc(self, columns=()):
		doc = frappe.new_doc("CRM Lead Import")
		doc.contract = self.contract
		for column in columns:
			doc.append("columns", column)
		return doc

	def test_a_column_mapped_inside_the_contract_saves(self):
		doc = self._doc([{"source_column": "Phone", "target_table": SECTION,
		                  "target_field": "zz_import_allowed"}])
		doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self.assertEqual(doc.status, "Draft")

	def test_a_column_outside_the_contract_is_refused_on_save(self):
		"""The seam ticked it into the catalogue but the contract never ticked it for this grain."""
		doc = self._doc([{"source_column": "X", "target_table": SECTION,
		                  "target_field": "zz_import_denied"}])
		with self.assertRaises(frappe.ValidationError) as caught:
			doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self.assertIn("zz_import_denied", str(caught.exception))

	def test_two_columns_mapped_to_one_field_are_refused(self):
		doc = self._doc([{"source_column": "A", "target_table": SECTION, "target_field": "zz_import_allowed"},
		                 {"source_column": "B", "target_table": SECTION, "target_field": "zz_import_allowed"}])
		with self.assertRaises(frappe.ValidationError):
			doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture

	def test_a_skipped_column_is_not_checked_against_the_contract(self):
		doc = self._doc([{"source_column": "X", "target_table": SECTION,
		                  "target_field": "zz_import_denied", "skip": 1}])
		doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self.assertEqual(doc.field_key_map(), {})

	def test_the_field_key_map_is_the_one_reader_of_the_grid(self):
		doc = self._doc([{"source_column": "Phone", "target_table": SECTION,
		                  "target_field": "zz_import_allowed"},
		                 {"source_column": "Ignored", "target_table": "", "target_field": ""}])
		doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self.assertEqual(doc.field_key_map(), {"Phone": self.allowed_key})

	def test_import_is_refused_before_any_validation(self):
		doc = self._doc()
		doc.status = "Draft"
		with self.assertRaises(frappe.ValidationError):
			doc.assert_importable()

	def test_import_is_refused_when_the_file_changed_after_validation(self):
		doc = self._doc()
		doc.status, doc.file_hash, doc.validated_against = "Validated", "new-bytes", "old-bytes"
		with self.assertRaises(frappe.ValidationError) as caught:
			doc.assert_importable()
		self.assertIn("changed", str(caught.exception).lower())

	def test_a_validated_import_whose_file_is_unchanged_may_run(self):
		doc = self._doc()
		doc.status, doc.file_hash, doc.validated_against = "Validated", "same-bytes", "same-bytes"
		doc.assert_importable()

	def test_replacing_the_file_clears_the_mapping_and_the_validation(self):
		doc = self._doc([{"source_column": "Phone", "target_table": SECTION,
		                  "target_field": "zz_import_allowed"}])
		doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		frappe.db.set_value("CRM Lead Import", doc.name,
		                    {"status": "Validated", "validated_against": "old", "valid_rows": 5},
		                    update_modified=False)
		doc.reload()
		doc.import_file = "/files/a-different-file.csv"
		doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self.assertEqual(doc.columns, [])
		self.assertEqual(doc.status, "Draft")
		self.assertIsNone(doc.validated_against)
		self.assertEqual(doc.valid_rows, 0)
