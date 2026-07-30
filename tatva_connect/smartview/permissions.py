"""Smart View visibility — the ONE brain for "may this caller use this view".

Sibling of `access/visibility.py` (which RECORDS a user may see) and `access/entitlement.py` (which
FIELDS). Same shape as both: one predicate here, two thin hook entry points wired in hooks.py, and every
consumer — the SPA endpoints, Desk, a report view — resolving through it rather than re-deciding.

A view reaches a caller three ways, asked in this order because only the LAST one is scoped:
  * it is THEIRS (`owner_user`).
  * it was SHARED with them, through frappe's own DocShare (`frappe.share.get_shared`). Nothing here
    restates what a share means, and a share is never grain-filtered — someone who could share it decided
    this person should have it, so scoping it away would make cross-line sharing silently do nothing.
  * it is STANDARD and its grain OVERLAPS the caller's entitlement — with a grain declaring NO axis at
    all meaning site-wide, offered to everyone and asked of nobody's entitlement. A saved view's grain is
    a RULE grain — authored, and free to leave an axis blank meaning ANY — so it is asked of
    `entitlement.grain_overlaps_entitlement`, never `covers`. Comparing that blank as the literal empty
    string is what offered a vertical-wide view on the tab row and then refused to open it, and the same
    shape once hid 129 fields from 1,894 leads.
WRITE is narrower: an operator, or the owner of a non-standard view. Sharing rides the WRITE gate —
you may hand on a view you may edit — which is the rule the endpoints already enforced.

WHY THE ENDPOINTS STAY THE GRANTING DOOR. `frappe.permissions.has_controller_permissions` (frappe
source, permissions.py:481) is explicit: *"Controllers can only deny permission, they can not explicitly
grant any permission that wasn't already present."* So the two hooks below cannot give a rep read access
to this doctype; the doctype keeps its System-Manager-only DocPerms and the whitelisted methods remain
the granting door (the app's server-scoped-writes invariant). The hooks are registered anyway as a
RESTRICTIVE backstop, so no native path — Desk, a link search, a report — can ever be looser than the
app door. `has_smart_view_permission` therefore denies only what this predicate positively refuses, and
never an operator or a DocShare recipient: `get_doc_permissions` (permissions.py:237) consults
controllers BEFORE role permissions and before frappe's own share fallback, so a wrong deny here would
break sharing site-wide.
"""
import frappe

from tatva_connect.access import visibility

SMART_VIEW_DT = "CRM Smart View"
# The columns the predicate reads. One list, so every caller fetches the same shape and no consumer
# hand-picks a subset the predicate then finds missing.
PREDICATE_FIELDS = ("name", "is_standard", "owner_user", "vertical", "group", "program")
# What the tab row renders on top of those. `column_widths` rides here deliberately: the grid applies it
# on its FIRST paint, and fetching it separately would render at defaults and then jump.
TAB_FIELDS = ("label", "base_object", "activity_type", "color", "icon", "view_order", "pinned",
              "column_widths")


def is_operator(user=None) -> bool:
	"""Privileged — Administrator or System Manager. One spelling, in the row-visibility brain."""
	return visibility.is_privileged(user)


def can_read(view, user=None, ctx=None) -> bool:
	"""May this caller OPEN the view — tab row, definition, rows, export and share list alike.

	THE RULE ITSELF LIVES IN `access/visibility.SCOPED["CRM Smart View"]`, which declares the three ways a
	view reaches a caller — `Own("owner_user")`, `Shared()`, and `RuleGrain(..., only_when="is_standard")`
	— and ORs them. This used to spell all three out here, including a third copy of the wildcard-grain
	rule that `CRM Workflow` and the row gate each had their own copy of.

	`ctx` is a `visibility.sweep_context`, the successor to this function's old `shared` argument and there
	for the same reason: a list scanning N views resolves DocShare once rather than N times. It is passed
	DOWN a sweep and never held — a memo that outlives the request answers with yesterday's shares.
	"""
	return visibility.row_admits(view, SMART_VIEW_DT, user, ctx=ctx)


def can_write(view, user=None) -> bool:
	"""May this caller EDIT the view — save, delete, share, column widths. Standard is operator-only."""
	user = user or frappe.session.user
	if is_operator(user):
		return True
	if view.get("is_standard"):
		return False
	return (view.get("owner_user") or None) == user


def readable_views(user=None):
	"""Every Smart View row this caller may read, ordered — the ONE list the tab row and the query
	conditions both come from, so Desk and the SPA can never offer different sets.

	`get_all` and a Python filter rather than SQL: the predicate's grain half is wildcard-aware and lives
	in `taxonomy.grain`, and expressing it a second time in SQL is precisely the second brain this module
	exists to remove. The table holds tens of rows, so one sweep is cheaper than a rule that could drift."""
	user = user or frappe.session.user
	rows = frappe.get_all(  # authz-ok: tier-b — gated row by row by can_read below; DocPerms are deliberately SM-only
		SMART_VIEW_DT,
		fields=[*PREDICATE_FIELDS, *TAB_FIELDS],
		order_by="view_order asc, label asc",
	)
	if is_operator(user):
		return rows
	ctx = visibility.sweep_context(SMART_VIEW_DT, user)  # DocShare resolved ONCE for the whole sweep
	return [r for r in rows if can_read(r, user, ctx=ctx)]


def get_smart_view_permission_query_conditions(user=None):
	"""The `permission_query_conditions` hook: a native list read narrowed to the same predicate.

	Composed from the SAME declaration `can_read` resolves through, so Desk and the SPA can never offer
	different sets — and the owner and share halves are now plain SQL rather than a name list built by
	scanning every row in Python."""
	return visibility.scoped_pqc(SMART_VIEW_DT, user)


def has_smart_view_permission(doc, ptype, user):
	"""The `has_permission` hook: the single-doc backstop. Deny-only (see the module docstring), so it
	answers True unless the predicate positively refuses. Read-shaped ptypes ask `can_read` — which
	already admits a DocShare recipient — and everything else asks `can_write`."""
	user = user or frappe.session.user
	if is_operator(user):
		return True
	if not doc or not doc.get("name"):
		return True
	if (ptype or "read") in ("read", "select", "print", "email", "export", "report"):
		return can_read(doc, user)
	return can_write(doc, user)
