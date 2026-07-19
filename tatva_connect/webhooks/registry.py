"""Single source of truth for CHANNELS and the providers that carry them.

Keyed by CHANNEL, not by vendor. Everything in an entry below is a property of the channel — the
doctype its accounts live in, where its ingress settings hang, which field names the vendor, what its
public URL looks like. A vendor contributes exactly one line: its name and its adapter's dotted path.
That is the whole cost of a second WhatsApp provider.

Keying by vendor was the original mistake and it spread: the vendor was in the registry key, in the
webhook URL, in the automation switch key, and in a second registry (`whatsapp/providers.py`) that
existed only because the first one could not answer "who sends for this account?". There is one
declaration now, and `channels.resolve` reads it.

Each entry describes a channel once:
  * channel         — the key, restated so a config dict can be passed around on its own
  * account_doctype — the doctype holding that channel's accounts
  * provider_field  — the field on an account row naming the vendor. The row is the ONLY source; a
                      URL host is a deployment detail, not an identity.
  * adapters        — provider name -> dotted path of its adapter module, lazy-imported
  * active_filter   — what makes one of its accounts live. A disabled account's token must not
                      authenticate, and doctypes spell "live" differently (a Check on one, a Select on
                      another), so it is declared rather than branched on.
  * ingress_prefix  — the fieldname prefix its account doctype uses for the ingress settings. Owned
                      doctypes carry the fields directly; an upstream doctype carries them as Custom
                      Fields, which Frappe requires to be `custom_`-prefixed. One knob, and the whole
                      auth surface (token, rotation, HMAC, IP allowlist) follows.
  * token_field     — the Password field carrying the per-account webhook secret
  * targets         — (host, account_doc, token) -> each URL to register, with the provider-dashboard
                      config it belongs to
"""

# Acefone binds one URL per trigger; `outbound_answered` has no trigger to bind to (its live-answered trigger is inbound-only), so that endpoint stays live but is never advertised.
_TELEPHONY_TARGETS = (
	("inbound_complete", "Inbound · Call hangup (Missed or Answered)", "required"),
	("outbound_complete", "Outbound · Call hangup (Missed or Answered) · Call Type: Click to call", "required"),
	("inbound_answered", "Inbound · Call answered by Agent", "optional — the call shows as In Progress while it is live"),
)

CHANNELS = {
	"whatsapp": {
		"channel": "whatsapp",
		"account_doctype": "WhatsApp Account",
		"provider_field": "custom_provider",
		"adapters": {"WATI": "tatva_connect.whatsapp.wati"},
		"active_filter": {"status": "Active"},
		"ingress_prefix": "custom_",
		"token_field": "custom_webhook_token",
		# No vendor in the path: the token resolves the account and the account names its provider, so
		# a tenant that moves from one vendor to another keeps the URL it already registered.
		"targets": lambda host, doc, token: [
			{
				"url": f"{host}/webhooks/whatsapp/{token}",
				"register_as": "Provider dashboard · Webhooks — one URL carries every event",
				"note": "required",
			}
		],
	},
	"telephony": {
		"channel": "telephony",
		"account_doctype": "CRM Telephony Account",
		"provider_field": "provider",
		"adapters": {"Acefone": "tatva_connect.telephony.adapters.acefone"},
		"active_filter": {"enabled": 1},
		"ingress_prefix": "",
		"token_field": "webhook_token",
		# Telephony keeps its provider segment: a provider binds one URL per trigger and the trailing
		# event is load-bearing, so the path is already per-provider shaped. The token still resolves
		# the account and the account still names the provider — the segment authenticates nothing.
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


def by_channel(channel):
	"""Channel config for a channel name, or None."""
	return CHANNELS.get(channel)


def by_account_doctype(account_doctype):
	"""Channel config for an account doctype, or None."""
	for cfg in CHANNELS.values():
		if cfg["account_doctype"] == account_doctype:
			return cfg
	return None


def providers_for(channel):
	"""The provider names registered on a channel."""
	return sorted((by_channel(channel) or {}).get("adapters") or {})
