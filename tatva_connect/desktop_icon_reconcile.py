"""Reconcile the LMS/Wiki desktop-icon rollout on every migrate (idempotent, isolated — A.19).

The fixture layer (desktop_icon/ + workspace_sidebar/) ships the "LMS"/"LMS Admin" and
"Wiki Docs"/"Wiki Editor" tiles, but two things a doctype JSON can't express:

  1. The admin-only children ("LMS Admin", "Wiki Editor") gate on their Workspace's `roles`
     table (Desktop Icon permission keys off the icon's label matching a Workspace Sidebar
     title, whose items are only visible if their target Workspace is itself permitted —
     see `frappe.desk.desktop.Workspace.is_permitted`). The "Learning" and "Wiki" workspaces
     ship with `roles` EMPTY (open to all) — this APPENDS (never removes) the roles that
     close that gap. Never fork lms/wiki (A.1) — this only adds Has Role rows.
  2. `create_desktop_icons_from_workspace` (core) auto-generates a standard=0, Administrator-
     owned, iconless top-level Desktop Icon for every public Workspace with no matching App
     icon — e.g. a stray top-level "Learning" tile. This hides that specific auto-orphan.
     NEVER touch a standard=1 icon (a real fixture) or one owned by a real user (a personal
     customization) — that scope is load-bearing, not incidental.
  3. The wiki app's own "Wiki" App tile is gated by `wiki.utils.check_app_permission`, which
     requires the `Wiki Manager` role — so plain readers never reach it and can't see the Wiki
     group under it. We surface the group under OUR OWN "Wiki Space" Folder tile instead (a
     Folder is always permitted; ships as a fixture, label "Wiki" so the children's label-match
     grouping lands on it). This hides the wiki App tile so there's no Wiki-Manager gate blocking
     readers and no duplicate "Wiki" tile for Wiki Managers. Hides only — never forks wiki (A.1).
"""

import frappe

# Workspace name -> roles it must carry (Has Role), mirroring who can create the underlying content:
# LMS Course (Course Creator/Moderator/System Manager), Wiki Page (Wiki Approver/System Manager).
_WORKSPACE_ROLES = {
	"Learning": ["Course Creator", "Moderator", "System Manager"],
	"Wiki": ["Wiki Approver", "System Manager"],
}

# Public workspaces we surface as branded Desktop Icon children; their auto-generated
# iconless top-level orphan (if any) must stay hidden.
_ORPHAN_ICON_NAMES = list(_WORKSPACE_ROLES.keys())


def reconcile():
	_gate_workspace_roles()
	_hide_orphan_icons()
	_hide_wiki_app_tile()
	# Roles/hidden flags above affect every user's cached tile list — drop it site-wide.
	frappe.cache.delete_key("desktop_icons")
	frappe.cache.delete_key("bootinfo")


def _gate_workspace_roles():
	try:
		for workspace, roles in _WORKSPACE_ROLES.items():
			if not frappe.db.exists("Workspace", workspace):
				continue
			existing = set(
				frappe.get_all(
					"Has Role", filters={"parent": workspace, "parenttype": "Workspace"}, pluck="role"
				)
			)
			missing = [role for role in roles if role not in existing]
			if not missing:
				continue
			doc = frappe.get_doc("Workspace", workspace)
			for role in missing:
				doc.append("roles", {"role": role})
			doc.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "desktop_icon_reconcile: gate_workspace_roles")


def _hide_orphan_icons():
	try:
		orphans = frappe.get_all(
			"Desktop Icon",
			filters={
				"standard": 0,
				"owner": "Administrator",
				"app": ["is", "not set"],
				"link_type": "Workspace Sidebar",
				"name": ["in", _ORPHAN_ICON_NAMES],
				"hidden": 0,
			},
			pluck="name",
		)
		for name in orphans:
			frappe.db.set_value("Desktop Icon", name, "hidden", 1)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "desktop_icon_reconcile: hide_orphan_icons")


def _hide_wiki_app_tile():
	"""Hide the wiki app's own "Wiki" App tile (gated by check_app_permission → Wiki Manager) so the
	Wiki group is reachable via our always-permitted "Wiki Space" Folder instead. Scoped tightly to
	the wiki-owned App icon; never touches our Folder, a child, or a user-owned icon. Idempotent."""
	try:
		wiki_tiles = frappe.get_all(
			"Desktop Icon",
			filters={"icon_type": "App", "app": "wiki", "name": "Wiki", "hidden": 0},
			pluck="name",
		)
		for name in wiki_tiles:
			frappe.db.set_value("Desktop Icon", name, "hidden", 1)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "desktop_icon_reconcile: hide_wiki_app_tile")
