# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Automations workspace + sidebar back to what the app ships.

The workflow-engine config surfaces (Action Groups, Workflows) and its queue/log views (Running
Instances, Signal Inbox, Step Log) were added to the workspace content, shortcuts, and sidebar. The
standard import skips a desk doc whose DB copy looks newer than its file, so a site whose Automations
desk was ever touched would keep the old layout and never show them &mdash; force both (same reason as
reimport_communications_desk).
"""
import frappe
from frappe.modules.import_file import import_file_by_path


def execute():
	for parts in (
		("tatva_connect", "workspace", "automations", "automations.json"),
		("workspace_sidebar", "automations.json"),
	):
		path = frappe.get_app_path("tatva_connect", *parts)
		try:
			import_file_by_path(path, force=True)
		except Exception:
			frappe.log_error(title=f"reimport {parts[0]} failed", message=frappe.get_traceback())

	frappe.clear_cache()
