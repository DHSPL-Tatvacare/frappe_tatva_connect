"""WhatsApp provider adapters — the common language.

One neutral spine (`WhatsApp Account` + `CRM WhatsApp Routing` + `CRM WhatsApp Settings`)
serves every provider; each account row carries `custom_provider`. This module maps a
provider value to the adapter module that speaks its API. Every outbound SEND path resolves
the adapter from the account and calls THROUGH it — so adding AISensy / Gupshup is: write an
adapter module exposing the surface below, register it here, add the Select option. No
call-site changes, no parallel spines, no parallel brains. (Inbound webhook parsing and
history pull are provider-specific modules — each provider brings its own — feeding the same
shared WhatsApp Message + routing contract.)

Adapter surface (duck-typed — already the shape of whatsapp/api.py):
    send_template_message · send_session_message · send_session_file ·
    send_session_file_via_url · get_media · normalize_number ·
    classify_send_response · template_param_names · is_enabled · assert_enabled

No silent default: an account whose provider has no registered adapter raises, so a
misconfigured account fails loud instead of sending through the wrong API (mirrors the
no-global-default routing rule).
"""
import frappe
from frappe import _

from tatva_connect.whatsapp import api as wati

# custom_provider value -> adapter module. WATI is the only provider today.
PROVIDERS = {"WATI": wati}


def provider_of(account) -> str:
	"""The account's provider. Falls back to WATI via the URL host marker for rows that
	predate the custom_provider field (the migration backfills them; this covers the gap)."""
	explicit = account.get("custom_provider")
	if explicit:
		return explicit
	return "WATI" if wati.is_wati_account(account) else ""


def has_adapter(account) -> bool:
	"""True if this account maps to a registered provider adapter (else the stock
	frappe_whatsapp Meta path runs — never reached for our WATI accounts)."""
	return provider_of(account) in PROVIDERS


def adapter_for(account):
	"""Return the provider adapter module for this WhatsApp Account, or raise."""
	provider = provider_of(account)
	adapter = PROVIDERS.get(provider)
	if adapter is None:
		frappe.throw(
			_("No WhatsApp adapter for provider '{0}' on account '{1}'.").format(
				provider or _("(unset)"), getattr(account, "name", account)
			),
			title=_("Unknown WhatsApp provider"),
		)
	return adapter
