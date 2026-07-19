"""Token expiry, made visible: a Facebook Page token lapses at about 60 days and the crawl then returns
nothing — no error, no leads, no signal. Graph knows the date, so it is asked once when the token is
saved and held on the source for the expiry Notification to read.
"""
from datetime import datetime, timezone

import frappe
from frappe.integrations.utils import make_get_request

from tatva_connect.lead_sync.graph import api_url, redact_tokens


def expiry_of(access_token: str):
	"""The token's expiry as a date, or None when Graph says it never expires or cannot be asked."""
	if not access_token:
		return None
	response = make_get_request(
		api_url("debug_token"), params={"input_token": access_token, "access_token": access_token}
	)
	expires_at = ((response or {}).get("data") or {}).get("expires_at")
	# Graph reports 0 for a token that does not expire (a System User token).
	if not expires_at:
		return None
	# Graph's expires_at is a UTC epoch; reading it in the host's local zone shifts the date a day either way.
	return frappe.utils.getdate(datetime.fromtimestamp(int(expires_at), tz=timezone.utc))


def stamp_expiry(source) -> None:
	"""Set token_expires_on from Graph. Never raises: an unaskable token must not block saving a source."""
	if not source.meta.has_field("token_expires_on"):
		return
	try:
		source.token_expires_on = expiry_of(source.get_password("access_token", raise_exception=False))
	except Exception:
		frappe.log_error(
			title=f"Facebook token expiry unknown: {source.name or source.type}",
			message=redact_tokens(frappe.get_traceback(with_context=True)),
		)
