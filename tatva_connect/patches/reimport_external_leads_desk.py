# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Land the External Leads workspace and retire the Partner API one it replaces.

Every source that writes a lead into the CRM from outside now sits under one roof — the partner API,
the Desk bulk import, the web intake forms and Facebook — because they are one concern wearing four
coats and were spread across three workspaces. Facebook Routing was filed under Communications, which
is a messaging channel, and Intake Forms under Field Operations.

The records are REPLACED rather than renamed: `Workspace` allows renaming but ships no `on_rename`, so
a rename would leave every reference pointing at a name that no longer exists. Four records move
together, because a Desk tile is a Link permitted only through a Workspace Sidebar OF THE SAME NAME
(desktop_icon_reconcile): Desktop Icon -> Workspace Sidebar -> Workspace. Rename the sidebar alone and
the tile is orphaned, which makes the whole space unreachable from the Desk.

The standard import skips a workspace whose DB copy looks newer than its file (import_file.py:141), so
a site whose desk was ever touched would keep the old groups and never see the new ones. The bumped
`modified` in each JSON covers a site that never diverged; this covers the ones that did. Same reason
and same shape as `reimport_infrastructure_desk`.
"""
import frappe
from frappe.modules.import_file import import_file_by_path

_RETIRED = (("Desktop Icon", "Partner API"), ("Workspace Sidebar", "Partner API"),
            ("Workspace", "Partner API"))

_REIMPORT = (
	("tatva_connect", "workspace", "external_leads", "external_leads.json"),
	("tatva_connect", "workspace", "communications", "communications.json"),
	("tatva_connect", "workspace", "field_operations", "field_operations.json"),
	("workspace_sidebar", "external_leads.json"),
	("workspace_sidebar", "field_operations.json"),
	("desktop_icon", "external_leads.json"),
)


def execute():
	for doctype, name in _RETIRED:
		if frappe.db.exists(doctype, name):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)  # authz-ok: tier-a — patch, runs at migrate

	for parts in _REIMPORT:
		path = frappe.get_app_path("tatva_connect", *parts)
		try:
			import_file_by_path(path, force=True)
		except Exception:
			frappe.log_error(title=f"reimport {parts[-1]} failed", message=frappe.get_traceback())

	frappe.clear_cache()
