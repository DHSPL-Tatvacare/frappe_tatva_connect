"""Rebuild the columns our fieldtype Property Setters widen."""
import frappe

# fieldname -> the information_schema data_type the Property Setter implies.
_EXPECTED = {
	"Facebook Lead Form Question": {"label": "text", "key": "text"},
	"Facebook Page": {"access_token": "text"},
	# An offloaded file's proxy URL runs past varchar(140); frappe's own File.file_url is Code for the same reason.
	"Insights Dashboard v3": {"preview_image": "longtext"},
	# core ships file_name at 140 and MariaDB REFUSES a longer one as a 500 with nothing stored; 255 is the ceiling every filesystem enforces.
	"File": {"file_name": "varchar(255)"},
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
	"""The column as `data_type`, or `varchar(n)` when the LENGTH is the thing the Property Setter moved.

	A widened `length` leaves data_type alone — varchar(140) and varchar(255) are both `varchar` — so a
	map that compared the type only would find nothing to do and the column would never follow the meta.
	Length is appended for varchar and nothing else, because text/longtext carry one too and appending it
	there would falsify every entry above that names a bare type."""
	row = frappe.db.sql(
		"""SELECT data_type, character_maximum_length FROM information_schema.columns
		   WHERE table_schema = %s AND table_name = %s AND column_name = %s""",
		(frappe.conf.db_name, f"tab{doctype}", column),
	)
	if not row:
		return ""
	data_type, length = (row[0][0] or "").lower(), row[0][1]
	return f"{data_type}({length})" if data_type == "varchar" and length else data_type
