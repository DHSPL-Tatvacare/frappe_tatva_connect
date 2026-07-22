# TEMPORARY — migration reconciliation demo, remove before prod.
# See tatva_connect/migration_check/REMOVE-ME.md
"""Portal controller for /migration_check/totals."""

import frappe

from tatva_connect.migration_check import guard

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/migration_check/totals"
		raise frappe.Redirect

	try:
		guard.assert_permitted()
	except guard.NotEnabled:
		raise frappe.DoesNotExistError from None

	context.no_cache = 1
	context.show_sidebar = False
	context.site_name = frappe.local.site
	return context
