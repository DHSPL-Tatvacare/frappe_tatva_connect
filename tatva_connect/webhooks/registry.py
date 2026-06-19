"""Single source of truth for inbound-webhook PROVIDERS.

The spine and the URL helper carry NO provider names of their own — they look a
provider up here. Adding a provider = one entry below + its adapter module + its
guest endpoint(s) + its account-doctype option. Nothing else in the trunk changes.

Each entry describes the provider once:
  * adapter         — dotted path of its adapter module (lazy-imported by the spine)
  * account_doctype — the doctype holding that provider's accounts
  * token_field     — the Password field carrying the per-account webhook secret
  * build_urls      — (host, account_doc, token) -> the pretty URL(s) to register
"""

# Acefone POSTs a distinct URL per call trigger; one webhook URL is registered per event.
_TELEPHONY_EVENTS = (
	"inbound_answered",
	"inbound_complete",
	"outbound_answered",
	"outbound_complete",
)

PROVIDERS = {
	"WATI": {
		"adapter": "tatva_connect.whatsapp.adapter",
		"account_doctype": "WhatsApp Account",
		"token_field": "custom_webhook_token",
		"build_urls": lambda host, doc, token: [f"{host}/webhooks/whatsapp/wati/{token}"],
	},
	"Acefone": {
		"adapter": "tatva_connect.telephony.adapter",
		"account_doctype": "CRM Telephony Account",
		"token_field": "webhook_token",
		"build_urls": lambda host, doc, token: [
			f"{host}/webhooks/telephony/{(doc.get('provider') or '').lower()}/{token}/{ev}"
			for ev in _TELEPHONY_EVENTS
		],
	},
}


def by_service(service):
	"""Provider config for a service name (e.g. 'WATI'), or None."""
	return PROVIDERS.get(service)


def by_account_doctype(account_doctype):
	"""Provider config for an account doctype (e.g. 'WhatsApp Account'), or None."""
	for cfg in PROVIDERS.values():
		if cfg["account_doctype"] == account_doctype:
			return cfg
	return None
