# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The workspace's `content` still names the OLD shortcut labels (`Step 1 - …`), so its four telephony tiles render empty — frappe looks a shortcut up by label and skips a miss. The standard import will not fix it (a workspace doc that looks newer than its file is skipped), and reimport_communications_desk has already run everywhere, so the corrected file needs a fresh forced import."""
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
