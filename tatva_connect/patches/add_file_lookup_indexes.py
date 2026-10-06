"""(content_hash, is_private, creation) and (is_attachments_folder, creation) on File — the two lookups frappe runs on every upload, neither of which had an index.

WHAT IT COSTS TODAY. `File.save_file` (file.py:833) asks for the newest File with this `content_hash` and
privacy before writing bytes, and `File.set_folder` (file.py:442) asks for the newest `is_attachments_folder`
row. Both read `ORDER BY creation DESC LIMIT 1` with nothing to seek on: measured on prod 2026-10-05, the
duplicate check read 221,585 rows in 4.2 s, and the folder lookup was running in four of six busy snapshots
— the attachments folder is one of the oldest rows, so walking `creation` newest-first reaches it last.

WHY COMPOSITE. `database/schema.py:311` drops a single-column index whose column does not declare
`search_index`, finding it by column, so a lone `content_hash` index would not survive the next sync. Each
index ends in `creation`, the read's own sort, so the seek also hands back rows already ordered.

ALGORITHM=INPLACE, LOCK=NONE for a secondary index: uploads continue while it builds. Idempotent (has_index
guard). Also in schema_setup._STEPS, because install-app baselines patches.txt without running it.
"""

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
