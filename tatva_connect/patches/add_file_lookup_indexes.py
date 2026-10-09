"""Index File on (content_hash, is_private, creation) and (is_attachments_folder, creation), the two reads frappe runs on every upload."""

import frappe

_DOCTYPE = "File"
_TABLE = "tabFile"
_INDEXES = (
	("ix_file_hash_private_creation", ("content_hash", "is_private", "creation")),
	("ix_file_attachments_folder_creation", ("is_attachments_folder", "creation")),
)


def execute():
	if not frappe.db.table_exists(_DOCTYPE):
		return
	for name, columns in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index(_DOCTYPE, list(columns), name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"files: index {name} failed")
