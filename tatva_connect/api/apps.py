# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Permission gate for the Tatva Connect app-launcher tile (/apps)."""

import frappe

from tatva_connect.access import user_admin


def check_app_permission() -> bool:
	"""Show the /apps tile to a System Manager or any app's manager; each child space still hides what its holder cannot read."""
	return bool(({"System Manager"} | user_admin.manager_roles()) & set(frappe.get_roles()))
