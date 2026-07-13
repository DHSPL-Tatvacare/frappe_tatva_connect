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
     group under it. We surface the group under OUR OWN "Wiki Space" tile instead (it carries no
     roles, so it is always permitted). This hides the wiki App tile so there's no Wiki-Manager
     gate blocking readers and no duplicate "Wiki" tile for Wiki Managers. Hides only — never
     forks wiki (A.1).

     "Wiki Space" is icon_type App, NOT Folder. `desktop.js:render_folder_thumbnail` only adds the
     `.folder-icon` class under `if (icon_type == "App")` nested inside `if (icon_type == "Folder")`
     — a dead branch — so a Folder's child grid renders at full size (374x450) inside a 54px
     overflow:hidden slot and is clipped to nothing. An App tile with children opens the same
     grouping modal (`setup_click` treats App and Folder alike) and renders its own logo.
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
	_assert_grouping_tiles_are_apps()
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
			doc.save(ignore_permissions=True)  # authz-ok: tier-a — schema/UI setup, runs at migrate
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


def _assert_grouping_tiles_are_apps():
	"""Force every tile of ours that groups children to icon_type App. A Folder renders as nothing.

	`desktop.js:render_folder_thumbnail` adds the `.folder-icon` class — the one that shrinks a folder's
	child grid into a thumbnail — inside `if (icon_type == "App")`, nested within `if (icon_type ==
	"Folder")`. That inner branch is unreachable, so the class is never applied, the child grid renders
	at full size (374x450) inside a 54px `overflow: hidden` slot, and is clipped away entirely. An App
	tile with children opens the same grouping modal (`setup_click` treats App and Folder alike) and
	renders its own logo, so App is the shape that works.

	Enforced here rather than left to the fixture: a standard doc is only re-imported when the FILE's
	`modified` is newer than the row's, so a row touched on the site (by an operator, or by any
	`db.set_value` that stamps `modified`) pins the old value and migrate skips it, silently. This runs
	unconditionally on every migrate and is idempotent.
	"""
	try:
		stale = frappe.get_all(
			"Desktop Icon",
			filters={"app": "tatva_connect", "icon_type": "Folder"},
			pluck="name",
		)
		for name in stale:
			frappe.db.set_value("Desktop Icon", name, "icon_type", "App", update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "desktop_icon_reconcile: assert_grouping_tiles_are_apps")


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
