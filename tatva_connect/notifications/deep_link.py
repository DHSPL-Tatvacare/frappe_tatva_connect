"""A notification's link opens the app the record lives in, never the Desk form behind it."""
import frappe
from frappe.utils.data import quoted

from tatva_connect import automation

AUTOMATION_KEY = "Notifications::Email::deep-links"

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


def point_at_the_app(doc, method=None):
	"""Fill the link for a record that has an app of its own; anything else keeps frappe's Desk form."""
	if doc.link or not doc.document_type or not doc.document_name:
		return
	link = app_url(doc.document_type, doc.document_name)
	if link and automation.is_enabled(AUTOMATION_KEY):
		doc.link = link
