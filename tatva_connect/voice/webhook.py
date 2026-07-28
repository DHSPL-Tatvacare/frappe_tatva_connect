"""The voice channel's inbound webhook — ONE guest endpoint, straight onto the shared spine.

Bolna registers a single webhook URL per agent and posts every execution event to it, so there is one
endpoint here and no event segment in the path. Everything before the adapter runs is the spine's:
token authentication + account identity (`webhooks.ingress.verify`), the kill-switch, the raw
Integration Request log, the fast ACK, and the deduplicated enqueue onto the worker.

Register on the provider (generate the token and copy the URL from the CRM AI Voice Account form):
    https://<host>/webhooks/voice/<token>
nginx rewrites that to this method with `?token=`, the same shape the WhatsApp webhook already uses.
The token both authenticates the caller and names the receiving account — auth and identity in one.
"""
import frappe
from frappe.rate_limiter import rate_limit

from tatva_connect.voice import channel
from tatva_connect.webhooks import spine

# A shared-tenant firehose needs a bound. Voice callbacks are one per call, so the cap is well clear of
# any real volume and exists to blunt abuse, not to shape traffic.
_RATE_LIMIT = 120


@frappe.whitelist(allow_guest=True)  # guest-ok: Bolna execution callback, no session — spine.receive verifies a per-account token before acting (A.16)
@rate_limit(limit=_RATE_LIMIT, seconds=60, ip_based=True)
def webhook(**kwargs):
	return spine.receive("voice", enabled=channel.is_enabled)
