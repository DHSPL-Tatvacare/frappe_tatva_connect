"""A notification's link opens the app the record lives in, never the Desk form behind it."""
import frappe

from tatva_connect import automation

AUTOMATION_KEY = "Notifications::Email::deep-links"

# Frappe links to the Desk form unless the log carries a link; helpdesk fills it for ticket assignments only.
ROUTES = {
	"HD Ticket": "/helpdesk/tickets/{name}",
	"CRM Lead": "/crm/leads/{name}",
	"CRM Deal": "/crm/deals/{name}",
}


def point_at_the_app(doc, method=None):
	"""Fill the link for a record that has an app of its own; anything else keeps frappe's Desk form."""
	if doc.link or not doc.document_type or not doc.document_name:
		return
	route = ROUTES.get(doc.document_type)
	if route and automation.is_enabled(AUTOMATION_KEY):
		doc.link = frappe.utils.get_url(route.format(name=doc.document_name))
