# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Communications workspace + sidebar back to what the app ships.

`merge_did_into_routing` dropped `CRM Telephony DID`, but the standard import skips a workspace whose
DB copy looks newer than its file, so such a site kept a shortcut and a sidebar item pointing at the
dead doctype — and the desk answers "DocType CRM Telephony DID not found". Re-import both, force.
"""
import frappe
from frappe.modules.import_file import import_file_by_path


def execute():
	for parts in (
		("tatva_connect", "workspace", "communications", "communications.json"),
		("workspace_sidebar", "communications.json"),
	):
		path = frappe.get_app_path("tatva_connect", *parts)
		try:
			import_file_by_path(path, force=True)
		except Exception:
			frappe.log_error(title=f"reimport {parts[0]} failed", message=frappe.get_traceback())

	frappe.clear_cache()
