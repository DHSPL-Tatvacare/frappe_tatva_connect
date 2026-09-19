# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""A Desk lead import: contract, file, column mapping, validation, import — in that order."""
import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect import tabular
from tatva_connect.access import entitlement
from tatva_connect.lead import mapping


class CRMLeadImport(Document):
	def validate(self):
		self._clamp_to_entitlement()
		self._read_new_file()
		self._validate_columns()
		self._drop_stale_validation()

	def onload(self):
		"""Refresh the contract's programme and source for the form, so rows saved before they were fetched read right."""
		if self.contract:
			contract = self.contract_doc()
			self.contract_program, self.contract_source = contract.program, contract.source

	def contract_doc(self):
		return frappe.get_cached_doc("CRM Lead API Mapping", self.contract)

	def _clamp_to_entitlement(self):
		"""The contract's grain must be one this operator holds."""
		contract = self.contract_doc()
		grain = {"vertical": contract.vertical, "group": contract.crm_group,
		         "program": contract.program or self.program}
		if not entitlement.grain_entitled(grain, frappe.session.user):
			frappe.throw(_("You are not entitled to the grain this contract carries."),
			             title=_("Outside your entitlement"))

	def _read_new_file(self):
		"""A new file resets the import and fills the grid from its header, in the save that attaches it."""
		before = self.get_doc_before_save()
		if (before.import_file if before else None) == self.import_file:
			return  # not `has_value_changed`: it is True for every field on insert
		self.columns = []
		self.validated_against = self.dry_run_job = self.import_job = None
		self.valid_rows = self.invalid_rows = self.row_count = 0
		self.status = "Draft"
		self.file_hash = None
		if self.import_file:
			self.file_hash = frappe.db.get_value("File", {"file_url": self.import_file}, "content_hash")
			self._columns_from_header()

	def _columns_from_header(self):
		"""One grid row per header; a header that is already a field key arrives mapped. Rows are counted, never parsed here."""
		headers, self.row_count = tabular.peek(*self.payload())
		known = {field["field_key"]: field for field in mapping.mappable_fields(contract=self.contract_doc())}
		for header in headers:
			field = known.get(header) or {}
			self.append("columns", {"source_column": header, "target_table": field.get("section"),
			                        "target_field": field.get("fieldname")})

	def _validate_columns(self):
		"""Every mapped column must name a field this contract may write, once."""
		allowed = {f["field_key"] for f in mapping.mappable_fields(contract=self.contract_doc())}
		seen = set()
		for row in self.columns or []:
			if row.skip or not row.target_field:
				continue
			if not row.target_table:
				frappe.throw(_("Column {0} names a field but no section.").format(row.source_column),
				             title=_("Section required"))
			key = f"{row.target_table}:{row.target_field}"
			if key not in allowed:
				frappe.throw(_("Column {0} is mapped to {1}, which this contract may not write.").format(
					row.source_column, key), title=_("Outside contract"))
			if key in seen:
				frappe.throw(_("Two columns are mapped to {0}.").format(key), title=_("Duplicate mapping"))
			seen.add(key)

	def _drop_stale_validation(self):
		"""A mapping edited after validation was never validated, so Import locks again."""
		before = self.get_doc_before_save()
		if before and self.status == "Validated" and before.field_key_map() != self.field_key_map():
			self.status, self.validated_against = "Draft", None

	def payload(self):
		"""The attached file as (bytes, format), read through its File row."""
		name = self.import_file and frappe.db.get_value("File", {"file_url": self.import_file}, "name")
		if not name:
			frappe.throw(_("Attach the file to import."), title=_("File required"))
		file = frappe.get_doc("File", name)
		fmt = (file.file_name or "").rsplit(".", 1)[-1].lower()
		if fmt not in tabular.FORMATS:
			frappe.throw(_("Attach a CSV or XLSX file."), title=_("Unsupported file"))
		return file.get_bytes(), fmt

	def field_key_map(self):
		"""{source_column: field_key} for the mapped columns — the one reader of the grid."""
		return {row.source_column: f"{row.target_table}:{row.target_field}"
		        for row in (self.columns or []) if row.target_field and not row.skip}

	def assert_importable(self):
		"""Import runs only on a validation of exactly the file now attached, in a Bulk Lane the operator chose."""
		if self.status != "Validated":
			frappe.throw(_("Validate the file before importing it."), title=_("Validation required"))
		if not self.validated_against or self.validated_against != self.file_hash:
			frappe.throw(_("The file changed after it was validated. Validate it again."),
			             title=_("Validation stale"))
		if not self.bulk_lane:
			frappe.throw(_("Choose the Bulk Lane before importing."), title=_("Bulk Lane required"))
