# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Close two stock workspaces that ship open to all: append the roles that may open them, re-asserted every migrate.

lms and wiki ship "Learning" and "Wiki" with `roles` empty. This only adds Has Role rows (never forks lms/wiki, A.1).
LMS Student is on Learning for landing, not authoring: lms's own app links to /desk/learning.
"""

import frappe

# Workspace name -> roles it must carry, mirroring who creates its content (LMS Course, Wiki Document).
_WORKSPACE_ROLES = {
	"Learning": ["Course Creator", "Batch Evaluator", "Moderator", "System Manager", "LMS Student"],
	"Wiki": ["Wiki Manager", "System Manager"],
}


def reconcile():
	try:
		for workspace, roles in _WORKSPACE_ROLES.items():
			if not frappe.db.exists("Workspace", workspace):
				continue
			existing = set(
				frappe.get_all("Has Role", filters={"parent": workspace, "parenttype": "Workspace"}, pluck="role")
			)
			missing = [role for role in roles if role not in existing]
			if not missing:
				continue
			doc = frappe.get_doc("Workspace", workspace)
			for role in missing:
				doc.append("roles", {"role": role})
			doc.save(ignore_permissions=True)  # authz-ok: tier-a — schema/UI setup, runs at migrate
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "workspace_roles: reconcile")
