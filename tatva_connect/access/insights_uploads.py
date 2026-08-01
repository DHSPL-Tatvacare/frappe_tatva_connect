# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Insights' spreadsheet import is off on this site.

Three endpoints carry it. `import_csv_data` is gated on the uploads data source, but
`get_columns_from_uploaded_file` and the legacy `import_csv` carry no data-source check at all, so
gating one leaves the other two answering. All three are refused here instead.

Why off: an imported table is named by the CLIENT and created with overwrite, so two people choosing
the same name silently clobber each other; and the browser uploads with no parent record, leaving a
File nothing owns and nothing ever deletes (invariant M1). Insights has no external data sources and
is not in real use, so removing the surface is cheaper than managing it.

This does NOT touch the file layer. `upload_file` is untouched, so CRM attachments, wiki assets and
lead documents upload, screen and offload exactly as before — the refusal is on the import, not the
upload. Residual: a file selected in the dialog is still created before the import is refused.

Signatures mirror the natives exactly; `tests/authz/test_app_load_guards.py` fails the build otherwise.
"""
import frappe
from frappe import _

_MESSAGE = "Uploading spreadsheets to Insights is disabled on this site. Connect a data source instead, or ask an administrator."
_TITLE = "Upload disabled"


def _refuse():
	frappe.throw(_(_MESSAGE), title=_(_TITLE))


@frappe.whitelist()
def import_csv_data(filename: str, tablename: str = ""):
	_refuse()


@frappe.whitelist()
def get_columns_from_uploaded_file(filename: str):
	_refuse()


@frappe.whitelist()
def import_csv(
	table_label: str, table_name: str, filename: str, if_exists: str, columns: list, data_source: str
):
	_refuse()
