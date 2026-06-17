# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Permission gate for the Tatva Connect app-launcher tile (/apps)."""

import frappe

# Mirror the roles on the "Tatva Connect" workspace — the tile must match who can open it.
_APP_ROLES = {"System Manager", "Sales Manager"}


def check_app_permission() -> bool:
	"""Show the /apps tile only to users who can see the Tatva Connect workspace."""
	return bool(_APP_ROLES & set(frappe.get_roles()))
