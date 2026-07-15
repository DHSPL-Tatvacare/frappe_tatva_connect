# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Partner API workspace back to what the app ships.

Phase 3 adds a `CRM Bulk Job` shortcut to the Partner API workspace, but the standard import skips a
workspace whose DB copy looks newer than its file (any site whose desk was ever touched), so the new
tile never appears. Re-import it, force. End state: the workspace matches the shipped file; safe twice.
"""
import frappe
from frappe.modules.import_file import import_file_by_path


def execute():
	path = frappe.get_app_path("tatva_connect", "tatva_connect", "workspace", "partner_api", "partner_api.json")
	try:
		import_file_by_path(path, force=True)
	except Exception:
		frappe.log_error(title="reimport partner_api desk failed", message=frappe.get_traceback())

	frappe.clear_cache()
