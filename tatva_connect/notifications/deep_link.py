"""A notification's link opens the app the record lives in, never the Desk form behind it."""
from urllib.parse import quote

import frappe
from frappe.utils.data import quoted

from tatva_connect.notifications import dispatch

# Frappe links to the Desk form unless the log carries a link; helpdesk fills it for ticket assignments only.
ROUTES = {
	"HD Ticket": "/helpdesk/tickets/{name}",
	"CRM Lead": "/crm/leads/{name}",
	"CRM Deal": "/crm/deals/{name}",
	"CRM Smart View": "/crm/smart-views?view={name}",
	"CRM Workflow": "/crm/workflows/{name}",
	"CRM Task Type": "/crm/task-forms/{name}",
}


def app_url(doctype, name):
	"""The app screen a record lives in, as a full URL, or None when it has only its Desk form."""
	route = ROUTES.get(doctype)
	return frappe.utils.get_url(route.format(name=quoted(name))) if route else None


# The app list a notice about many records opens; every list reads a `?filters=` drill.
LISTS = {"CRM Lead": "/crm/leads/view/list", "CRM Deal": "/crm/deals/view/list", "CRM Task": "/crm/tasks/view/list"}


def list_url(doctype, filters):
	"""The app list of `doctype` drilled to `filters`, as a full URL; None when it has no app list or notices are off."""
	route = LISTS.get(doctype)
	if not route or not dispatch.armed():
		return None
	return frappe.utils.get_url(route + "?filters=" + quote(frappe.as_json(filters, indent=None, separators=(",", ":"))))


def point_at_the_app(doc, method=None):
	"""Fill the link for a record that has an app of its own; anything else keeps frappe's Desk form."""
	if doc.link or not doc.document_type or not doc.document_name:
		return
	link = app_url(doc.document_type, doc.document_name)
	if link and dispatch.armed():
		doc.link = link
