"""Telephony provider adapters — the common language.

One neutral spine (CRM Telephony Account + CRM Telephony Routing + CRM Telephony Settings)
serves every provider; each account row carries `provider`. This module maps a provider
value to the adapter module that speaks its API. The shared outbound entry (bridge.make_a_call)
resolves the adapter from the routed account and places the call through it, so adding a
telephony provider is: write an adapter module exposing the surface below, register it here,
add the Select option. No call-site changes, no parallel spines, no parallel brains.

Adapter surface (duck-typed — already the shape of telephony/api.py):
    click_to_call · get_call_report · normalize_number · base_url_of ·
    is_enabled · assert_enabled

No silent default: an account whose provider has no registered adapter raises, so a
misconfigured account fails loud instead of dialling through the wrong API.
"""
import frappe
from frappe import _

from tatva_connect.telephony import api as acefone

# provider value -> adapter module. Acefone is the only provider today.
PROVIDERS = {"Acefone": acefone}


def provider_of(account) -> str:
	"""The account's provider. Defaults to Acefone for rows created before the field
	existed (the migration backfills them)."""
	return account.get("provider") or "Acefone"


def has_adapter(account) -> bool:
	return provider_of(account) in PROVIDERS


def adapter_for(account):
	"""Return the provider adapter module for this Telephony Account, or raise."""
	provider = provider_of(account)
	adapter = PROVIDERS.get(provider)
	if adapter is None:
		frappe.throw(
			_("No telephony adapter for provider '{0}' on account '{1}'.").format(
				provider or _("(unset)"), getattr(account, "name", account)
			),
			title=_("Unknown telephony provider"),
		)
	return adapter
