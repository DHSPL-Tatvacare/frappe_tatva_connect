# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Land the CRM Configuration workspace and retire the Field Operations one it replaces.

Replaced, not renamed, for the reason `reimport_external_leads_desk` records: `Workspace` ships no
`on_rename`, and a Desk tile is a Link permitted only through a Workspace Sidebar of the same name, so
Desktop Icon -> Workspace Sidebar -> Workspace move together. Import before delete, so a failed import
keeps the old space reachable.
"""
import frappe

from tatva_connect.patches import _desk

_RETIRED = (("Desktop Icon", "Field Operations"), ("Workspace Sidebar", "Field Operations"),
            ("Workspace", "Field Operations"))

_REPLACEMENT = (
	("tatva_connect", "workspace", "crm_configuration", "crm_configuration.json"),
	("workspace_sidebar", "crm_configuration.json"),
	("desktop_icon", "crm_configuration.json"),
)


def execute():
	landed = all([_desk.reimport(*parts) for parts in _REPLACEMENT])  # a list, so every file is attempted

	if landed:
		for doctype, name in _RETIRED:
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)  # authz-ok: tier-a — patch, runs at migrate
	else:
		frappe.log_error(
			title="replace field operations desk: replacement did not land",
			message="The CRM Configuration workspace/sidebar/icon did not all import, so Field Operations was KEPT rather than leaving the Desk with neither.",
		)

	frappe.clear_cache()
