# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""A Desk lead import: contract, file or Google Sheet, column mapping, validation, import — in that order."""
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.csvutils import get_csv_content_from_google_sheets

from tatva_connect import tabular
from tatva_connect.access import entitlement
from tatva_connect.lead import mapping


class CRMLeadImport(Document):
	def validate(self):
		self._hold_while_running()
		self._clamp_to_entitlement()
		self._one_source()
		self._read_new_source()
		self._validate_columns()
		self._drop_stale_validation()

	def onload(self):
		"""The contract's programme and source for the form, whether it has a source, and the one action offered now."""
		from tatva_connect.lead_import import api

		if self.contract:
			contract = self.contract_doc()
			self.contract_program, self.contract_source = contract.program, contract.source
		self.set_onload("has_source", bool(self.data_source()))
		self.set_onload("next_stage", api.next_stage(self))

	def _hold_while_running(self):
		"""A run reads this import as it goes, so nothing on it changes until the run ends or is stopped."""
		from tatva_connect.lead_import import api

		before = self.get_doc_before_save()
		if before and before.status in api.RUNNING:
			frappe.throw(_("This import is {0}. Wait for it to end, or stop it, before changing it.").format(
				_(before.status)), title=_("Run in progress"))

	def contract_doc(self):
		return frappe.get_cached_doc("CRM Lead API Mapping", self.contract)

	def _clamp_to_entitlement(self):
		"""The grain this import writes to must be one this operator holds."""
		from tatva_connect.lead_import import creator

		mp = creator.bound_grain(self)
		grain = (mp.vertical or "", mp.crm_group or "", mp.program or "")
		if not entitlement.grain_entitled(grain, frappe.session.user):
			frappe.throw(_("You are not entitled to the grain this contract carries."),
			             title=_("Outside your entitlement"))

	def _one_source(self):
		"""An import reads one source, so what was validated is never in doubt."""
		if self.import_file and self.google_sheets_url:
			frappe.throw(_("Attach a file or give a Google Sheets URL, not both."), title=_("One source"))

	def _read_new_source(self):
		"""A new source resets the import and fills the grid from its header, in the save that sets it."""
		before = self.get_doc_before_save()
		if (before.data_source() if before else None) == self.data_source():
			return  # not `has_value_changed`: it is True for every field on insert
		self.columns = []
		self.file_hash = self.validated_against = self.dry_run_job = self.import_job = None
		self.valid_rows = self.invalid_rows = self.row_count = 0
		self.status = "Draft"
		if self.data_source():
			self._columns_from_header()

	def _columns_from_header(self):
		"""One grid row per header, a known field key arriving mapped; rows are counted, never parsed here."""
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
			key = _field_key(row)
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

	def data_source(self):
		"""What this import reads: its Google Sheet or its attached file, or None."""
		return self.google_sheets_url or self.import_file

	def payload(self):
		"""The source as (bytes, format): the sheet through frappe's own reader, a file through its File row."""
		if self.google_sheets_url:
			return get_csv_content_from_google_sheets(self.google_sheets_url), "csv"
		file = self._file()
		if not file:
			frappe.throw(_("Attach the file to import."), title=_("File required"))
		fmt = (file.file_name or "").rsplit(".", 1)[-1].lower()
		if fmt not in tabular.FORMATS:
			frappe.throw(_("Attach a CSV or XLSX file."), title=_("Unsupported file"))
		return file.get_bytes(), fmt

	def field_key_map(self):
		"""{source_column: field_key} for the mapped columns — the one reader of the grid."""
		return {row.source_column: _field_key(row) for row in (self.columns or []) if row.target_field and not row.skip}

	def _file(self):
		"""The attached file's File row, or None: its bytes are read through it, so storage decides where they live."""
		name = self.import_file and frappe.db.get_value("File", {"file_url": self.import_file}, "name")
		return frappe.get_doc("File", name) if name else None

	def assert_importable(self, digest):
		"""Import runs only on the very bytes that were validated (`digest`), in a Bulk Lane the operator chose."""
		if not self.validated_against or self.validated_against != digest:
			frappe.throw(_("The file changed after it was validated. Validate it again."),
			             title=_("Validation stale"))
		if not self.bulk_lane:
			frappe.throw(_("Choose the Bulk Lane before importing."), title=_("Bulk Lane required"))


def _field_key(row):
	"""A mapped column's catalog key: its section and field."""
	return f"{row.target_table}:{row.target_field}"
