# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CSV and XLSX, read and written, in one place — frappe's own readers and writers, nothing hand-rolled.

Extracted from `partner_bulk_worker._csv_records`, whose rule was already right: the header keys each
row, and a row whose column count does not match becomes a per-record failure rather than a record
silently shifted into the wrong columns. It lives here so both directions and every consumer share it —
the Desk bulk import reads through it, a template is written through it, and a bulk export will write
through it without the rule being stated a second time.

Cell values pass through untouched apart from stripping a string. A CSV cell is already text; an XLSX
cell arrives as the number or date openpyxl read, and the ORM coerces that better than this seam could
coerce it to text and back.
"""
import frappe
from frappe import _
from frappe.utils.csvutils import build_csv_response, read_csv_content, to_csv
from frappe.utils.xlsxutils import build_xlsx_response, make_xlsx, read_xlsx_file_from_attached_file

FORMATS = ("csv", "xlsx")

_SHEET = "Data"  # the single sheet a generated workbook carries; one lead per row needs no second one


def read(raw, fmt):
	"""Header-keyed dicts from csv or xlsx bytes."""
	return _to_dicts(_rows(raw, fmt))


def write(header, rows, fmt):
	"""Bytes for a download, in the caller's format."""
	if fmt == "csv":
		return to_csv([header, *rows]).encode("utf-8")
	if fmt == "xlsx":
		return make_xlsx([header, *rows], _SHEET).getvalue()
	_refuse(fmt)


def respond(header, rows, fmt, filename):
	"""Emit a download through frappe's own response builders — the browser gets a native file."""
	if fmt == "csv":
		build_csv_response([header, *rows], filename)
		return
	if fmt == "xlsx":
		build_xlsx_response([header, *rows], filename)
		return
	_refuse(fmt)


def _rows(raw, fmt):
	if fmt == "csv":
		return read_csv_content(raw) or []
	if fmt == "xlsx":
		return read_xlsx_file_from_attached_file(fcontent=raw) or []
	_refuse(fmt)


def _to_dicts(rows):
	"""The ONE dict-ising rule — a ragged row names itself instead of shifting into the wrong columns."""
	rows = [r for r in rows if r and any(not _blank(c) for c in r)]
	if not rows:
		return []
	header = [str(h).strip() for h in rows[0]]
	out = []
	for row in rows[1:]:
		if len(row) != len(header):
			out.append({"__error__": _("row has {0} columns, expected {1}").format(len(row), len(header))})
		else:
			out.append(dict(zip(header, [_cell(c) for c in row], strict=False)))
	return out


def _cell(value):
	"""A string is stripped; anything else (a number, a date) is handed on as openpyxl read it."""
	return value.strip() if isinstance(value, str) else value


def _blank(value):
	return value is None or (isinstance(value, str) and not value.strip())


def _refuse(fmt):
	frappe.throw(_("{0} is not a supported file format; use {1}.").format(fmt, " or ".join(FORMATS)),
	             title=_("Unsupported format"))
