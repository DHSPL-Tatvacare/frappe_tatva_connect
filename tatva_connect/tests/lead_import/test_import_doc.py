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

	def test_an_entitled_user_who_is_not_system_manager_can_save_an_import(self):
		"""RED before: the grain went to `grain_entitled` as a dict, so everyone but System Manager was refused."""
		frappe.set_user(PARTNER)
		try:
			self._doc().insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture; validate's own grain clamp is what runs
		finally:
			frappe.set_user("Administrator")

	def _header(self, **kwargs):
		from tatva_connect.lead_import import api

		imp = self._doc()
		imp.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		api.download_template(imp.name, fmt="csv", **kwargs)
		return frappe.response["result"].splitlines()[0].replace('"', "").split(",")

	def test_a_template_holds_only_chosen_fields_the_contract_takes_and_always_the_identity(self):
		from tatva_connect.lead_import import api

		identity = [key for key in self._header() if api._is_identity(key)]
		self.assertEqual(len(identity), 1)
		self.assertEqual(self._header(keys=frappe.as_json([self.denied_key])), identity)
		self.assertEqual(sorted(self._header(keys=frappe.as_json([self.allowed_key]))), sorted(identity + [self.allowed_key]))

	def _mapped_import(self, status):
		"""A saved import with a mapped file, moved to `status` the way the queue and the job end move it."""
		doc = self._doc()
		doc.import_file = self._csv(f"{self.allowed_key}\n+919876543210\n")
		doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		frappe.db.set_value("CRM Lead Import", doc.name, "status", status)
		doc.reload()
		return doc

	def test_every_declared_status_offers_one_action_and_every_endpoint_obeys_it(self):
		"""RED before: the server started a validation from any status. Statuses are read off the schema, so a new one cannot slip past."""
		from tatva_connect.lead_import import api

		endpoints = {"validate": api.start_validation, "import": api.start_import, "stop": api.stop_import}
		for status in frappe.get_meta("CRM Lead Import").get_field("status").options.split("\n"):
			doc = self._mapped_import(status)
			offered = api.next_stage(doc)
			for action, endpoint in endpoints.items():
				if action == offered:
					self.assertEqual(api._ready(doc.name, action).name, doc.name)  # the gate alone: running the stage would queue a real job
					continue
				with self.assertRaises(frappe.ValidationError, msg=f"{status}: {action}") as caught:
					endpoint(doc.name)
				frappe.clear_messages()
				self.assertIn(f"cannot {action} now", str(caught.exception), f"{status}: {action} (offered {offered})")

	def test_a_new_file_is_refused_while_a_run_is_in_flight(self):
		"""RED before: the swap reset the import, and the old run's end then stamped Validated on the new file."""
		for status in ("Validating", "Importing"):
			doc = self._mapped_import(status)
			doc.import_file = self._csv("Other Header\na\n")
			with self.assertRaises(frappe.ValidationError, msg=status) as caught:
				doc.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture
			self.assertIn("Wait for it to end", str(caught.exception))

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
