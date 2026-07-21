# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""A Desk lead import: a grain declaration, a file, a column mapping, and a mandatory validation gate.

The grain is never typed and never read off the sheet. It is taken from the contract and clamped to what
the operator is entitled to, by the same brain that scopes every other surface, and `_force_routing`
stamps it on every row — so a `vertical` column in a spreadsheet reaches nothing.

The save-time column check asks `lead/mapping.py`, which is the same seam that fed the picker its
options. Picker and validator therefore cannot disagree: there is one list, asked twice.
"""
import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.access import entitlement
from tatva_connect.lead import mapping


class CRMLeadImport(Document):
	def validate(self):
		self._clamp_to_entitlement()
		self._validate_columns()
		self._reset_on_new_file()

	def _clamp_to_entitlement(self):
		"""The contract must carry a grain this operator holds — the clamp every surface shares."""
		contract = frappe.get_cached_doc("CRM Lead API Mapping", self.contract)
		grain = {"vertical": contract.vertical, "group": contract.crm_group,
		         "program": self.program or contract.program}
		if not entitlement.grain_entitled(grain, frappe.session.user):
			frappe.throw(_("You are not entitled to the grain this contract carries."),
			             title=_("Outside your entitlement"))

	def _validate_columns(self):
		"""The backstop for a mapping that never opened the picker, or opened it before the contract moved."""
		contract = frappe.get_cached_doc("CRM Lead API Mapping", self.contract)
		allowed = {f["field_key"] for f in mapping.mappable_fields(contract=contract)}
		seen = set()
		for row in self.columns or []:
			if row.skip or not row.target_field:
				continue
			if not row.target_table:
				frappe.throw(_("Column {0} names a field but no section.").format(row.source_column),
				             title=_("Section required"))
			key = f"{row.target_table}:{row.target_field}"
			if key not in allowed:
				frappe.throw(
					_("Column {0} is mapped to {1}, which this grain may not write.").format(
						row.source_column, key),
					title=_("Outside contract"))
			if key in seen:
				frappe.throw(_("Two columns are mapped to {0}; the second would overwrite the first.").format(key),
				             title=_("Duplicate mapping"))
			seen.add(key)

	def _reset_on_new_file(self):
		"""A replaced file invalidates the mapping and the validation — neither describes the new bytes."""
		if not self.has_value_changed("import_file"):
			return
		self.columns = []
		self.validated_against = None
		self.valid_rows = self.invalid_rows = self.row_count = 0
		self.dry_run_job = self.import_job = None
		self.status = "Draft"
		self.file_hash = self._attachment_hash()

	def _attachment_hash(self):
		"""The content hash frappe already stores on the File row — read, never recomputed."""
		if not self.import_file:
			return None
		return frappe.db.get_value("File", {"file_url": self.import_file}, "content_hash")

	def field_key_map(self):
		"""{source_column: field_key} for the mapped columns — the ONE reader of the column grid."""
		return {row.source_column: f"{row.target_table}:{row.target_field}"
		        for row in (self.columns or []) if row.target_field and not row.skip}

	def assert_importable(self):
		"""Import is refused unless a validation ran against exactly the bytes now attached."""
		if self.status != "Validated":
			frappe.throw(_("The file must be validated before it is imported."),
			             title=_("Validation required"))
		if not self.validated_against or self.validated_against != self.file_hash:
			frappe.throw(_("The file changed after it was validated. Validate it again."),
			             title=_("Validation stale"))
