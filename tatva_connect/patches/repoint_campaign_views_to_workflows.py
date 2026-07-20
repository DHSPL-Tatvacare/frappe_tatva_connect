"""Re-point saved views from the `Campaigns` route to `Workflows`.

W8 renamed the surface: the route, its name and the sidebar label are Workflows now. A saved CRM View
Settings row still naming `Campaigns` resolves to a route that no longer exists, so the view opens on
nothing — and a user's own saved view failing is not something they can fix themselves.

Declared end state: no view row names the old route. Idempotent, and a clean no-op on a site where no
one ever saved a view over this doctype (which is every fresh install).
"""
import frappe

OLD, NEW = "Campaigns", "Workflows"
VIEW_DT = "CRM View Settings"


def execute():
	if not frappe.db.exists("DocType", VIEW_DT):
		return
	if "route_name" not in frappe.db.get_table_columns(VIEW_DT):
		return  # the fork's view row does not carry a route — nothing to re-point
	for name in frappe.get_all(VIEW_DT, filters={"route_name": OLD}, pluck="name"):
		frappe.db.set_value(VIEW_DT, name, "route_name", NEW)  # authz-ok: tier-c — patch, no user input
