# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

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

	def on_update(self):
		ingress.sync_token_digests(self)
