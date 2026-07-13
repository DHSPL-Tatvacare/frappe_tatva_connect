"""Single source of truth for inbound-webhook PROVIDERS.

The spine, the ingress gate and the URL helper carry no provider names of their own — a provider is
looked up here. Adding one is an entry below plus its adapter module, its guest endpoint and its
account-doctype option. Nothing else in the trunk changes.

Each entry describes a provider once:
  * adapter         — dotted path of its adapter module, lazy-imported by the spine
  * account_doctype — the doctype holding that provider's accounts
  * active_filter   — what makes one of its accounts live. A disabled account's token must not
                      authenticate, and providers spell "live" differently (a Check on ours, a Select
                      on an upstream one), so it is declared rather than branched on.
  * ingress_prefix  — the fieldname prefix its account doctype uses for the ingress settings.
                      Owned doctypes carry the fields directly; an upstream doctype carries them as
                      Custom Fields, which Frappe requires to be `custom_`-prefixed. One knob, and
                      the whole auth surface (token, rotation, HMAC, IP allowlist) follows.
  * token_field     — the Password field carrying the per-account webhook secret
  * targets         — (host, account_doc, token) -> each URL to register, with the provider-dashboard config it belongs to
"""

# Acefone binds one URL per trigger; `outbound_answered` has no trigger to bind to (its live-answered trigger is inbound-only), so that endpoint stays live but is never advertised.
_TELEPHONY_TARGETS = (
	("inbound_complete", "Inbound · Call hangup (Missed or Answered)", "required"),
	("outbound_complete", "Outbound · Call hangup (Missed or Answered) · Call Type: Click to call", "required"),
	("inbound_answered", "Inbound · Call answered by Agent", "optional — the call shows as In Progress while it is live"),
)

PROVIDERS = {
	"WATI": {
		"adapter": "tatva_connect.whatsapp.adapter",
		"account_doctype": "WhatsApp Account",
		"active_filter": {"status": "Active"},
		"ingress_prefix": "custom_",
		"token_field": "custom_webhook_token",
		"targets": lambda host, doc, token: [
			{
				"url": f"{host}/webhooks/whatsapp/wati/{token}",
				"register_as": "WATI dashboard · Webhooks — one URL carries every event",
				"note": "required",
			}
		],
	},
	"Acefone": {
		"adapter": "tatva_connect.telephony.adapters.acefone",
		"account_doctype": "CRM Telephony Account",
		"active_filter": {"enabled": 1},
		"ingress_prefix": "",
		"token_field": "webhook_token",
		"targets": lambda host, doc, token: [
			{
				"url": f"{host}/webhooks/telephony/{(doc.get('provider') or '').lower()}/{token}/{ev}",
				"register_as": register_as,
				"note": note,
			}
			for ev, register_as, note in _TELEPHONY_TARGETS
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
