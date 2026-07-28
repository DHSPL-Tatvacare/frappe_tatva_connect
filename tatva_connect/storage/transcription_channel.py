# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The transcription channel's kill-switch and its ONE guest endpoint, straight onto the shared spine.

Everything before the adapter runs is the spine's: token authentication and account identity
(`webhooks.ingress.verify`), the kill-switch, the raw Integration Request log, the fast ACK and the
deduplicated enqueue onto the worker. Nothing about inbound auth is written here, because nothing about
it is new — a transcription service is another account row.

Register on the service (generate the token and copy the URL from the CRM Transcription Account form):
    https://<host>/webhooks/transcription/<token>
nginx rewrites that to this method with `?token=`, the same shape the WhatsApp and voice webhooks use.
The token both authenticates the caller and names the receiving account.
"""
import frappe
from frappe.rate_limiter import rate_limit

from tatva_connect.automation import settings
from tatva_connect.webhooks import spine

# An unknown key reads False, so the channel is dormant until an operator creates and ticks the row.
SWITCH_INBOUND = "Transcription::Channel::inbound"

# One post per recorded call, arriving in batches after a night's work. Well clear of any real volume.
_RATE_LIMIT = 300


def is_enabled() -> bool:
	"""The transcription channel's kill-switch. Dormant by default — OFF until explicitly enabled."""
	return settings.is_enabled(SWITCH_INBOUND)


@frappe.whitelist(allow_guest=True)  # guest-ok: transcription service POST, no session — spine.receive verifies a per-account token before acting (A.16)
@rate_limit(limit=_RATE_LIMIT, seconds=60, ip_based=True)
def webhook(**kwargs):
	return spine.receive("transcription", enabled=is_enabled)
