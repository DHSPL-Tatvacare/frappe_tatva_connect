# TEMPORARY — migration reconciliation demo, remove before prod.
# See tatva_connect/migration_check/REMOVE-ME.md
"""Portal page controller for /migration_check.

Login-gated, never public: a Guest is redirected to login, and a logged-in user without the role
gets a 403. On a site without the config keys the route 404s, so the page does not advertise its
own existence in production.
"""

import frappe

from tatva_connect.migration_check import guard

no_cache = 1


def get_context(context):
	# Guest first: a login prompt is the right answer, not a 404.
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/migration_check"
		raise frappe.Redirect

	try:
		guard.assert_permitted()
	except guard.NotEnabled:
		# Not enabled here — behave as if the page does not exist.
		raise frappe.DoesNotExistError from None

	context.no_cache = 1
	context.show_sidebar = False
	context.site_name = frappe.local.site
	return context
