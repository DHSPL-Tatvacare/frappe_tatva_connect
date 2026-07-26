# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Automations workspace back to what the app ships.

Phase 11 deleted the guard lane: no workflow action can block a rep's save any more. Step 3 of the guided
text still told the operator the opposite, and a page that describes a rule the engine no longer has is worse
than no text. The standard import SKIPS a workspace whose DB copy looks newer than its file
(`import_file.py:141`), which is true on every site whose desk was ever opened, so the bumped `modified`
alone would migrate silently green and change nothing.
"""
import frappe
from frappe.modules.import_file import import_file_by_path


def execute():
	path = frappe.get_app_path("tatva_connect", "tatva_connect", "workspace", "automations", "automations.json")
	try:
		import_file_by_path(path, force=True)
	except Exception:
		frappe.log_error(title="reimport automations workspace failed", message=frappe.get_traceback())

	frappe.clear_cache()
