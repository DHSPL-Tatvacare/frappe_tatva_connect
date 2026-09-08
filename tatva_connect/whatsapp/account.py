# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_account.whatsapp_account import WhatsAppAccount

from tatva_connect.webhooks import ingress


class ChannelWhatsAppAccount(WhatsAppAccount):
	"""Upstream WhatsApp Account, plus the shared webhook-ingress contract.

	Inbound authentication is infrastructure, not an automation: it must never become a switch an
	operator can turn off. It therefore hangs off the class override rather than `doc_events`, where
	the drift guard requires every handler to be a registered, toggleable automation.

	`CRM Telephony Account` gets the same two calls from its own controller. One contract, wired the
	same way on both account doctypes.
	"""

	def validate(self):
		ingress.assert_ingress_config(self)
		warn_if_multi_number_undeclared(self)

	def on_update(self):
		ingress.sync_token_digests(self)


def warn_if_multi_number_undeclared(doc, method=None):
	"""Say so when a row shares a provider account with another and has not declared it.

	The declaration is what puts this row's own number on the wire and scopes its history read to its own
	thread. Left blank on a shared account, the provider falls back to that account's DEFAULT number — one
	brand's message goes out under another brand's name, and both the send and the read answer success, so
	nothing downstream can notice.

	IT WARNS AND DOES NOT BLOCK, deliberately. Sharing a URL is not proof of a mistake: a decommissioned
	row, a second environment or a fixture can share one legitimately, and refusing the save would make
	this check the arbiter of a fact only the operator knows. It also cannot see the whole picture — the
	SIBLING may be the row that needs ticking, and it is not the row being saved. So it tells the operator
	what it sees, at the moment they can act on it, and leaves the declaration theirs.
	"""
	url = (doc.get("url") or "").rstrip("/")
	if not url or doc.get("custom_wati_multi_number"):
		return
	sibling = next(
		(
			row.account_name or row.name
			for row in frappe.get_all("WhatsApp Account", fields=["name", "account_name", "url"])
			if row.name != doc.name and (row.url or "").rstrip("/") == url
		),
		None,
	)
	if sibling:
		frappe.msgprint(
			_("{0} uses the same provider account as this one. If this row is one of several numbers on "
			  "it, tick 'WATI Account Has More Than One Number' — otherwise messages sent from here leave "
			  "from the provider's default number instead of {1}.").format(
				sibling, doc.get("custom_wati_channel_number") or _("this number")
			),
			title=_("Shared provider account"),
			indicator="orange",
		)
