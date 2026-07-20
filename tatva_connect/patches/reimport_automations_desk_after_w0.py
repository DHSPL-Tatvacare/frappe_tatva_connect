# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Automations desk back to what the app ships, after the W0 model rename.

W0 renamed CRM Workflow Definition/Instance/Signal to CRM Workflow/Run/Event and deleted CRM Action
Group outright — a node owns its actions now. The workspace and sidebar still pointed at all four, and
the desk answers "DocType ... not found" for a shortcut whose target is gone.

A new patch rather than an edit to `reimport_automations_desk`: that one has already run, and an
applied patch is dead — editing it fixes nothing on a site that ran it.

Force is required even with the timestamp bumped, because `import_file.py` skips a standard doc whose
DB row looks newer than the file, so on any site whose desk was ever touched the fix would migrate
silently green and change nothing.
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
			frappe.log_error(title=f"reimport {parts[-1]} failed", message=frappe.get_traceback())

	frappe.clear_cache()
