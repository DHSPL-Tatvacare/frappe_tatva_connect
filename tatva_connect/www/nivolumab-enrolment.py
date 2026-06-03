import frappe

# Public enrolment page — no login. The page only renders the form; the write
# happens server-side via tatva_connect.api.enrolment.nivolumab_enrolment.
no_cache = 1


def get_context(context):
	context.no_cache = 1
	context.csrf_token = frappe.local.session.data.get("csrf_token", "") if frappe.local.session else ""
	return context
