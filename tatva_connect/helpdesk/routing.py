"""Which team and which source a ticket inherits from the account it arrived through, read from `HD Ticket Routing`."""
import frappe

from tatva_connect.helpdesk import ROUTING

EMAIL_ACCOUNT = "Email Account"


def route_of(account_doctype, account):
	"""The enabled row for this account, or None. The row's key IS the account, so one account has one answer."""
	name = f"{account_doctype}::{account}"
	if not frappe.db.exists(ROUTING, {"name": name, "enabled": 1}):
		return None
	return frappe.get_cached_doc(ROUTING, name)


def stamp(doc):
	"""Copy the team and the source the ticket's own account declares; a ticket with no routing row is left as it is."""
	if not doc.email_account:
		return
	route = route_of(EMAIL_ACCOUNT, doc.email_account)
	if not route:
		return
	if not doc.agent_group and route.agent_group:
		doc.agent_group = route.agent_group
	if not doc.custom_ticket_source and route.ticket_source:
		doc.custom_ticket_source = route.ticket_source
