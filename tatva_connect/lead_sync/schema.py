"""Rebuild the columns our fieldtype Property Setters widen."""
import frappe

# fieldname -> the information_schema data_type the Property Setter implies.
_EXPECTED = {
	"Facebook Lead Form Question": {"label": "text", "key": "text"},
	"Facebook Page": {"access_token": "text"},
	# An offloaded file's proxy URL runs past varchar(140); frappe's own File.file_url is Code for the same reason.
	"Insights Dashboard v3": {"preview_image": "longtext"},
}


def reconcile_fieldtypes():
	"""A fixture Property Setter moves the meta, never the column: sync_all's updatedb runs before sync_fixtures, and the unchanged doctype JSON skips it every migrate after."""
	for doctype, columns in _EXPECTED.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		if all(_column_type(doctype, column) == expected for column, expected in columns.items()):
			continue
		frappe.reload_doctype(doctype, force=True)


def _column_type(doctype: str, column: str) -> str:
	row = frappe.db.sql(
		"""SELECT data_type FROM information_schema.columns
		   WHERE table_schema = %s AND table_name = %s AND column_name = %s""",
		(frappe.conf.db_name, f"tab{doctype}", column),
	)
	return (row[0][0] if row else "").lower()
