# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Communications workspace back to what the app ships, for the Facebook Routing section.

A bumped `modified` alone does not ship a workspace on a site whose desk was ever touched: import_file
skips a standard file whose timestamp is not newer than the DB row, so the edit migrates silently green
and changes nothing. Re-import, force — the same reason reimport_communications_desk exists.
"""
import frappe
from frappe.modules.import_file import import_file_by_path


def execute():
	path = frappe.get_app_path("tatva_connect", "tatva_connect", "workspace", "communications", "communications.json")
	try:
		import_file_by_path(path, force=True)
	except Exception:
		frappe.log_error(title="reimport communications (facebook) failed", message=frappe.get_traceback())
	frappe.clear_cache()
