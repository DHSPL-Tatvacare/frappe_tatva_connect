"""Where a ticket started: the mailbox it arrived at says so, and the source row of that name is written if the operator keeps one."""
import frappe

from tatva_connect.helpdesk import SOURCE

EMAIL = "Email"


def stamp(doc):
	"""An email ticket carries the account it arrived at; nothing else fills `email_account`. A source the operator has not created is left blank rather than invented."""
	if doc.custom_ticket_source or not doc.email_account:
		return
	if frappe.db.exists(SOURCE, {"name": EMAIL, "disabled": 0}):
		doc.custom_ticket_source = EMAIL
