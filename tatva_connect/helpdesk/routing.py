"""Which team, source and grain a ticket inherits from the account it arrived through, read from `HD Ticket Routing`."""
import frappe

from tatva_connect.helpdesk import ROUTING, SOURCE
from tatva_connect.taxonomy import grain

EMAIL_ACCOUNT = "Email Account"
MAIL_SOURCE = "Email"


def route_of(account_doctype, account):
	"""The row in force for this account, or None. The row's key IS the account, so one account has one answer."""
	name = f"{account_doctype}::{account}"
	if not frappe.db.exists(ROUTING, {"name": name, "disabled": 0}):
		return None
	return frappe.get_cached_doc(ROUTING, name)


def stamp(doc):
	"""Copy the team, the grain and the source the ticket's own account declares; mail with nothing to say is still mail."""
	if not doc.email_account:
		return
	route = route_of(EMAIL_ACCOUNT, doc.email_account)
	if route and not doc.agent_group and route.agent_group:
		doc.agent_group = route.agent_group
	# Only a ticket that names no grain yet takes the account's, so one that arrived with its own keeps it.
	if route and not any(grain.own_of(doc)):
		grain.stamp(doc, grain.own_of(route))
	if doc.custom_ticket_source:
		return
	if route and route.ticket_source:
		doc.custom_ticket_source = route.ticket_source
	elif frappe.db.exists(SOURCE, {"name": MAIL_SOURCE, "disabled": 0}):
		doc.custom_ticket_source = MAIL_SOURCE
