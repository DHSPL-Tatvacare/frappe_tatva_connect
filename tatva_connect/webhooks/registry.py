"""Single source of truth for inbound-webhook PROVIDERS.

The spine, the ingress gate and the URL helper carry no provider names of their own — a provider is
looked up here. Adding one is an entry below plus its adapter module, its guest endpoint and its
account-doctype option. Nothing else in the trunk changes.

Each entry describes a provider once:
  * adapter         — dotted path of its adapter module, lazy-imported by the spine
  * account_doctype — the doctype holding that provider's accounts
  * ingress_prefix  — the fieldname prefix its account doctype uses for the ingress settings.
                      Owned doctypes carry the fields directly; an upstream doctype carries them as
                      Custom Fields, which Frappe requires to be `custom_`-prefixed. One knob, and
                      the whole auth surface (token, rotation, HMAC, IP allowlist) follows.
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
		"ingress_prefix": "custom_",
		"token_field": "custom_webhook_token",
		"build_urls": lambda host, doc, token: [f"{host}/webhooks/whatsapp/wati/{token}"],
	},
	"Acefone": {
		"adapter": "tatva_connect.telephony.adapters.acefone",
		"account_doctype": "CRM Telephony Account",
		"ingress_prefix": "",
		"token_field": "webhook_token",
		"build_urls": lambda host, doc, token: [
			f"{host}/webhooks/telephony/{(doc.get('provider') or '').lower()}/{token}/{ev}"
			for ev in _TELEPHONY_EVENTS
		],
	},
}


def by_service(service):
	"""Provider config for a service name, or None."""
	return PROVIDERS.get(service)


def by_account_doctype(account_doctype):
	"""Provider config for an account doctype, or None."""
	for cfg in PROVIDERS.values():
		if cfg["account_doctype"] == account_doctype:
			return cfg
	return None
