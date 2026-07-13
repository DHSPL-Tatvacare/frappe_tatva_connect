"""Desk tiles: how a Desktop Icon is permitted and drawn. READ THIS BEFORE EDITING desktop_icon/.

Two upstream bugs and one gate decide whether a tile appears and whether it draws anything. They were
each found the hard way, and every one of them will bite the next person who adds a tile.


THE THREE SHAPES (frappe/desk/doctype/desktop_icon/desktop_icon.py::get_desktop_icons)

    if   icon_type == "Folder":  permitted = True
    elif icon_type == "App":     permitted = check_app_permission(label, app)
    else:                        permitted = bool(sidebar and sidebar["items"])   # a Link

  Folder — permitted for EVERYONE, and DRAWS NOTHING. `desktop.js::render_folder_thumbnail` adds the
    `.folder-icon` class (the one that shrinks the child grid to a thumbnail) inside
    `if (icon_type == "App")` NESTED WITHIN `if (icon_type == "Folder")` — an unreachable branch. The
    class is never applied, so the child grid renders at its full 374x450 inside a 54x53
    `overflow: hidden` slot and is clipped away entirely. Measured in the DOM. Folders are broken for
    every Frappe site; we were simply the only ones using one. NEVER use Folder.

  App — DRAWS, but the permission is the OWNING APP'S, not the tile's. `check_app_permission(label,
    app)` matches on `app == a` and then calls THAT app's `add_to_apps_screen.has_permission`. Our
    tiles carry app "tatva_connect", whose gate (`tatva_connect.api.apps.check_app_permission`) is
    System/Sales Manager. So ANY tatva_connect tile made an App becomes admin-only, whatever it is.
    Correct for "Tatva Connect" and "Tatva Titan". Fatal for anything a rep must see.

  Link — DRAWS, and the permission is the tile's own. A Link is permitted when a Workspace Sidebar
    TITLED THE SAME AS THE TILE holds an item the user may see (`boot.get_sidebar_items` ->
    `desk_views.is_item_allowed`). And that is the lever:

        item link_type "URL"        -> is_item_allowed returns True unconditionally  -> EVERYONE
        item link_type "Workspace"  -> only if that Workspace is permitted            -> BY ROLE

    This is the ONLY shape that both draws and lets permission be set per tile. Every tile of ours
    that a non-admin must see is a Link with a matching Workspace Sidebar. "Wiki Space" is a Link
    whose sidebar carries a URL item to the handbook, so every rep sees it; "Communications",
    "Automations", "Partner API" and the rest are Links whose sidebars carry Workspace items, so they
    resolve by role. One mechanism, no special cases.

  NOTE: `Desktop Icon.roles` is a dead field. `get_desktop_icons` filters on `standard`/`owner` only
  and never reads it. Do not try to gate a tile with it.


WHO GETS THE DESK AT ALL

  The "Framework" tile is frappe's own, gated by `frappe.permissions.check_app_permission`, which
  demands `System Manager` — not Desk User, not System User. Of the four role profiles only CRM Admin
  carries it, so only a CRM Admin sees the Desk. A rep or a manager sees CRM, Learning and Wiki Space
  and nothing else. That is correct: System Manager administers users, roles and permissions.


WHAT THIS MODULE FIXES, AND WHY A FIXTURE CANNOT

  A standard doc is re-imported only when the FILE's `modified` beats the ROW's. Any row touched on the
  site — by an operator, or by any `db.set_value` that stamps `modified` — pins the old value and
  migrate skips it in silence. Everything below is therefore re-asserted on every migrate, idempotent.

  1. The admin-only children ("LMS Admin", "Wiki Editor") gate on their Workspace's `roles` table. The
     "Learning" and "Wiki" workspaces ship with `roles` EMPTY (open to all); this APPENDS the roles
     that close that gap. Never forks lms/wiki (A.1) — it only adds Has Role rows.
  2. `create_desktop_icons_from_workspace` (core) auto-generates a standard=0, Administrator-owned,
     iconless top-level Desktop Icon for every public Workspace with no matching App icon. This hides
     that auto-orphan. NEVER touch a standard=1 icon (a real fixture) or one owned by a real user.
  3. The wiki app's own "Wiki" App tile is gated by `wiki.utils.check_app_permission`, which demands
     `Wiki Manager`, so a reader never reaches it. It is hidden here and our own "Wiki Space" Link
     tile carries the handbook instead. Hides only — never forks wiki (A.1).
  4. "Wiki Space" is held at icon_type Link, for the reasons above.
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
	_assert_wiki_space_is_a_link()
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


def _assert_wiki_space_is_a_link():
	"""Hold the Wiki tile at icon_type Link. Neither of the other two shapes can carry it.

	`get_desktop_icons` permits a tile in exactly one of three ways:

	    Folder  ->  always permitted, and `desktop.js:render_folder_thumbnail` never applies the
	                `.folder-icon` class (its `if (icon_type == "App")` sits inside `if (icon_type ==
	                "Folder")` — unreachable), so the child grid renders at full size inside a 54px
	                overflow:hidden slot and is clipped to nothing. Visible to all, draws nothing.
	    App     ->  `check_app_permission(label, app)` matches on `app == a` and calls THAT app's gate.
	                The tile carries app "tatva_connect", so it inherits our own admin gate and is
	                denied to every rep. Draws, but only admins see it.
	    Link    ->  permitted when a Workspace Sidebar titled the same as the tile has an item the user
	                may see. `is_item_allowed` returns True unconditionally for a URL item, so the
	                "Wiki Space" sidebar's handbook link admits everyone. Draws, and the permission is
	                the sidebar's — per role, not per app.

	Only Link is both. Enforced here rather than left to the fixture: a standard doc is re-imported only
	when the FILE's `modified` beats the row's, so a row touched on the site pins the old value and
	migrate skips it in silence. Idempotent.
	"""
	try:
		if not frappe.db.exists("Desktop Icon", "Wiki Space"):
			return
		current = frappe.db.get_value("Desktop Icon", "Wiki Space", "icon_type")
		if current != "Link":
			frappe.db.set_value(
				"Desktop Icon", "Wiki Space", {"icon_type": "Link", "link_type": "External"},
				update_modified=False,
			)
			frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "desktop_icon_reconcile: assert_wiki_space_is_a_link")


def _hide_wiki_app_tile():
	"""Hide the wiki app's own "Wiki" App tile (gated by check_app_permission → Wiki Manager) so the
	handbook is reachable via our own "Wiki Space" Link tile instead. Scoped tightly to the wiki-owned
	App icon; never touches our tile, a child, or a user-owned icon. Idempotent."""
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
