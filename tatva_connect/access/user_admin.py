# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Who may add a colleague and which roles they may grant, read off every app's `user_invitation.allowed_roles` hook."""
import frappe
from frappe import _

from tatva_connect.access import ledger, visibility


def _allowed_roles():
	"""frappe merges the hook across installed apps, so each manager role maps to the union of what every app lets it grant."""
	return (frappe.get_hooks("user_invitation") or {}).get("allowed_roles") or {}


def manager_roles():
	"""Every app's manager role — the ledger's declaration, the one list lockdown also grants from."""
	return set(ledger.MANAGER_ROLES)


def grantable_roles(user=None):
	"""The roles this caller may grant: the union over each manager role they hold."""
	held = set(frappe.get_roles(user or frappe.session.user))
	return {role for manager, roles in _allowed_roles().items() if manager in held for role in roles}


def assert_may_grant(user_doc, roles):
	"""Refuse a non-privileged save that grants a role outside the caller's reach or changes a System Manager's account."""
	if user_doc.flags.ignore_permissions or visibility.is_privileged():
		return
	if not user_doc.is_new() and visibility.is_privileged(user_doc.name):
		frappe.throw(_("Only a System Manager may change a System Manager's account."), frappe.PermissionError)
	refused = set(roles) - grantable_roles()
	if refused:
		frappe.throw(
			_("You may not grant these roles: {0}").format(", ".join(sorted(refused))), frappe.PermissionError
		)


def assert_may_rename():
	"""Renaming changes the login address a password reset goes to, so only a System Manager renames an account."""
	if not visibility.is_privileged():
		frappe.throw(_("Only a System Manager may rename an account."), frappe.PermissionError)
