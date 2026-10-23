"""Whose work a viewer may see — the CRM's own Sales Hierarchy subtree, read the way crm's lead permission reads it.

One answer for every team view (Rep Load, Near Me's My day): a System Manager, or a manager outside the tree, sees
every rep; a manager inside the tree sees their subtree (crm.permissions.org_hierarchy._permission_query_conditions).
"""
import frappe
from crm.permissions.org_hierarchy import _in_hierarchy, _team_mem_query, hierarchy_enabled

# The roles a team view opens to — Rep Load's own report roles.
MANAGER_ROLES = ("System Manager", "Sales Manager")


def team_of(user):
	"""None for a viewer who sees every rep; their hierarchy subtree for a manager inside it."""
	if "System Manager" in frappe.get_roles(user) or not (hierarchy_enabled() and _in_hierarchy(user)):
		return None
	return _team_mem_query(user).run(pluck=True)


def is_manager(user):
	return bool(set(MANAGER_ROLES) & set(frappe.get_roles(user)))


def may_view(viewer, user):
	"""Whether `viewer` may see `user`'s own work: themselves always; a manager their team (everyone, when outside the tree)."""
	if viewer == user:
		return True
	if not is_manager(viewer):
		return False
	team = team_of(viewer)
	return team is None or user in team
