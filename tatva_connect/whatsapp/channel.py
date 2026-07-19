"""The WhatsApp CHANNEL — everything true of WhatsApp whoever carries it.

A switch is a property of the channel, not of the vendor behind it. `WhatsApp::WATI::messaging` said
otherwise, and it meant an operator who changed provider would silently lose their own configuration:
the new vendor's key defaults OFF, so WhatsApp would go dark on migrate with nothing in the log to say
why. The keys are `WhatsApp::Channel::messaging|templates|backfill` — `Channel` is the subject because
they gate the whole channel, not one vendor — and the per-vendor control is the one that genuinely
belongs to a vendor: an account's Active/Inactive status.

Address format is the channel's too. E.164 digits is how WhatsApp names a subscriber; that is not a
WATI convention and a second provider will not spell it differently.
"""
import re

import frappe
from frappe import _

from tatva_connect import automation

CHANNEL = "whatsapp"

# The operator toggles. Vendor-free by construction — see the module docstring.
SWITCH_MESSAGING = "WhatsApp::Channel::messaging"
SWITCH_TEMPLATES = "WhatsApp::Channel::templates"
SWITCH_BACKFILL = "WhatsApp::Channel::backfill"
SWITCH_RECOVERY = "WhatsApp::Channel::recovery"


def is_enabled() -> bool:
	"""The WhatsApp master kill-switch. Dormant by default — OFF until explicitly enabled (a blank or
	unsaved single reads as disabled). It stops send AND receive."""
	return automation.is_enabled(SWITCH_MESSAGING)


def assert_enabled():
	"""Block sends while the kill-switch is off."""
	if not is_enabled():
		frappe.throw(
			_("WhatsApp is switched off (CRM Tatva Automation → {0}). No messages are sent.").format(
				SWITCH_MESSAGING
			),
			title=_("WhatsApp disabled"),
		)


def normalize_number(number: str) -> str:
	"""Canonicalise a phone number to bare E.164 digits (strip +, -, spaces).

	Stock frappe_whatsapp.format_number only strips a leading '+', leaving hyphens like
	'+91-7753022190' that providers reject. We always use this.
	"""
	return re.sub(r"\D", "", number or "")
