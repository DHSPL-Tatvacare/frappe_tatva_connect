# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The import document: grain clamped, mapping backstopped, file read on attach, import gated on a validation."""
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
		doc.bulk_lane = "Quiet"
		doc.assert_importable()

	def test_import_is_refused_until_a_bulk_lane_is_chosen(self):
		doc = self._doc()
		doc.status, doc.file_hash, doc.validated_against = "Validated", "same-bytes", "same-bytes"
		with self.assertRaises(frappe.ValidationError) as caught:
			doc.assert_importable()
		self.assertIn("Bulk Lane", str(caught.exception))

	def _csv(self, body, ext="csv"):
		"""A real private File row: the doc reads bytes through the File, never a path."""
		f = frappe.get_doc({"doctype": "File", "file_name": f"zz-import-{frappe.generate_hash(length=8)}.{ext}",
		                    "is_private": 1, "content": body.encode("utf-8")})
		f.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		return f.file_url

	def test_attaching_a_file_fills_the_grid_from_its_header_in_that_save(self):
		doc = self._doc()
		doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		doc.import_file = self._csv(f"{self.allowed_key},Unknown Header\n+919876543210,x\n+919876543211,y\n")
		doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self.assertEqual(doc.row_count, 2)
		self.assertEqual([(c.source_column, c.target_table, c.target_field) for c in doc.columns],
		                 [(self.allowed_key, SECTION, "zz_import_allowed"), ("Unknown Header", None, None)])

	def test_replacing_the_file_rereads_the_grid_and_clears_the_validation(self):
		doc = self._doc()
		doc.import_file = self._csv(f"{self.allowed_key}\n+919876543210\n")
		doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		frappe.db.set_value("CRM Lead Import", doc.name,
		                    {"status": "Validated", "validated_against": "old", "valid_rows": 5},
		                    update_modified=False)
		doc.reload()
		doc.import_file = self._csv("Other Header\na\nb\nc\n")
		doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self.assertEqual([c.source_column for c in doc.columns], ["Other Header"])
		self.assertEqual(doc.row_count, 3)
		self.assertEqual(doc.status, "Draft")
		self.assertIsNone(doc.validated_against)
		self.assertEqual(doc.valid_rows, 0)

	def test_a_dry_run_with_refused_rows_still_validates_so_the_rest_can_load(self):
		from tatva_connect.lead_import.api import _terminal_state
		job = frappe._dict(dry_run=1, status="JobComplete", succeeded=5, failed=2)
		state = _terminal_state(job, frappe._dict(file_hash="bytes"))
		self.assertEqual((state["status"], state["validated_against"], state["invalid_rows"]),
		                 ("Validated", "bytes", 2))

	def test_a_dry_run_where_no_row_passed_fails_validation(self):
		from tatva_connect.lead_import.api import _terminal_state
		job = frappe._dict(dry_run=1, status="JobComplete", succeeded=0, failed=7)
		state = _terminal_state(job, frappe._dict(file_hash="bytes"))
		self.assertEqual((state["status"], state["validated_against"]), ("Validation Failed", None))

	def test_a_file_that_is_not_csv_or_xlsx_is_refused_on_attach(self):
		doc = self._doc()
		doc.import_file = self._csv("a,b\n1,2\n", ext="xls")
		with self.assertRaises(frappe.ValidationError) as caught:
			doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self.assertIn("CSV or XLSX", str(caught.exception))

	def test_editing_the_mapping_after_validation_locks_import_again(self):
		doc = self._doc()
		doc.import_file = self._csv(f"{self.allowed_key},Unknown Header\n+919876543210,x\n")
		doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		frappe.db.set_value("CRM Lead Import", doc.name, {"status": "Validated", "validated_against": doc.file_hash},
		                    update_modified=False)
		doc.reload()
		doc.columns[0].skip = 1
		doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		self.assertEqual((doc.status, doc.validated_against), ("Draft", None))
