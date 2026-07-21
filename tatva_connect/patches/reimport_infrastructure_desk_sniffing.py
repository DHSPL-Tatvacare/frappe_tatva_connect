# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Infrastructure workspace + sidebar back to what the app ships, after byte matching moved.

Step 2 of the Storage guided section promised that "the signatures a type must really begin with" were set
on the screening form. They are not: the hand-written table was deleted and the check now runs on frappe's
own `filetype`, which needs nothing configured and cannot fall out of date. A step that names a field the
form no longer has is worse than no step at all. The sidebar is re-asserted in the same pass because the
two ship together and their order is the narrative.

`reimport_infrastructure_desk_storage` has already run wherever this matters, and an applied patch is dead
(rule 3) — so this ships as its own line and re-asserts the same declared end state.
"""
import frappe
from frappe.modules.import_file import import_file_by_path


def execute():
	for parts in (
		("tatva_connect", "workspace", "infrastructure", "infrastructure.json"),
		("workspace_sidebar", "infrastructure.json"),
	):
		path = frappe.get_app_path("tatva_connect", *parts)
		try:
			import_file_by_path(path, force=True)
		except Exception:
			frappe.log_error(title=f"reimport {parts[-1]} failed", message=frappe.get_traceback())

	frappe.clear_cache()
