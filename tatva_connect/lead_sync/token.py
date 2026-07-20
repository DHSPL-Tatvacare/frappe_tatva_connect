"""The Facebook credential model, in one place.

Three token kinds matter. A SHORT user token is what Graph API Explorer hands an operator and it lasts
an hour or two. A LONG user token lasts 60 days and is minted by exchanging a short one against the app
secret. A PAGE token derived from a long user token does not expire, and it is the credential the crawl
runs on, so a lapsed user token stops nothing once bootstrap has happened.

The operator pastes the short token. Everything below that is the server's job.
"""
from datetime import datetime, timezone

import frappe
from frappe.utils.password import get_decrypted_password

from tatva_connect.lead_sync.graph import api_url, graph_get, graph_post, redact_tokens

SETTINGS = "CRM Facebook Settings"


def app_credentials() -> tuple[str, str]:
	"""The app id and secret the exchange and the token inspection are made against."""
	settings = frappe.get_cached_doc(SETTINGS)
	return (settings.app_id or ""), (settings.get_password("app_secret", raise_exception=False) or "")


def token_info(access_token: str) -> dict:
	"""Graph's own account of a token: type, app, expiry, scopes. THE debug_token call site, so every
	consumer reads one shape. An app token authenticates the question when the app secret is configured,
	which is also the only way Graph will describe a token that has already lapsed."""
	if not access_token:
		return {}
	app_id, app_secret = app_credentials()
	inspector = f"{app_id}|{app_secret}" if (app_id and app_secret) else access_token
	response = graph_get("token inspection", api_url("debug_token"), {"input_token": access_token}, inspector)
	return (response or {}).get("data") or {}


def expiry_date(info: dict):
	"""The expiry date carried by a debug_token payload, or None when it does not expire.
	Kept pure so a caller holding the payload does not ask Graph a second time for the same fact."""
	expires_at = (info or {}).get("expires_at")
	# Graph reports 0 for a token that does not expire: a derived Page token, or a System User token.
	if not expires_at:
		return None
	# Graph's expires_at is a UTC epoch; reading it in the host's local zone shifts the date a day either way.
	return frappe.utils.getdate(datetime.fromtimestamp(int(expires_at), tz=timezone.utc))


def stops_working_on(info: dict):
	"""The date this credential stops carrying a crawl, or None when nothing bounds it.

	Two separate clocks bound a Facebook token and the earlier one is what matters. `expires_at` is the
	token's own life and is 0 for a token Meta issued without one. `data_access_expires_at` is the 90-day
	re-engagement window, and it applies EVEN to a token that never expires: on that date Graph stops
	returning the person's data and the crawl goes quiet with no error. Watching only the first is how a
	non-expiring token still stops delivering leads unannounced."""
	dates = [
		expiry_date(info),
		expiry_date({"expires_at": (info or {}).get("data_access_expires_at")}),
	]
	real = [d for d in dates if d]
	return min(real) if real else None


def is_short(info: dict) -> bool:
	"""True when the token dies within a day, which is what an Explorer token does and a 60-day one does not."""
	expiry = expiry_date(info)
	return bool(expiry) and frappe.utils.date_diff(expiry, frappe.utils.nowdate()) <= 1


def refresh_credential(source) -> None:
	"""Bring the stored token to its durable form and record when it lapses. THE save-time seam.

	Graph is inspected ONCE per save and the payload is reused, so a save costs one call rather than one
	per question asked of it. Never raises: an unaskable token, a Graph outage or a refused exchange must
	not stop an operator saving a source, and the Validate Token report is where each shows up."""
	access_token = source.get_password("access_token", raise_exception=False)
	if not access_token:
		return
	try:
		info = token_info(access_token)
		if is_short(info):
			long_token = exchange_for_long_lived(access_token)
			if long_token:
				source.access_token = long_token
				info = token_info(long_token)
		if source.meta.has_field("token_expires_on"):
			source.token_expires_on = stops_working_on(info)
	except Exception:
		frappe.log_error(
			title=f"Facebook credential refresh failed: {source.name or source.type}",
			message=redact_tokens(frappe.get_traceback(with_context=True)),
		)


def exchange_for_long_lived(short_token: str) -> str:
	"""Trade a short user token for the 60-day one. Returns "" when the app credentials are not configured."""
	app_id, app_secret = app_credentials()
	if not (short_token and app_id and app_secret):
		return ""
	# An OAuth endpoint, not a Graph read: the credentials ARE the payload and the token is the subject of
	# the request, so nothing here authorizes it and no bearer header is sent. POSTed rather than sent as
	# query params, so the App Secret never enters a URL that a proxy, an access log or a retry can echo.
	response = graph_post(
		"long-lived token exchange",
		api_url("oauth/access_token"),
		{
			"grant_type": "fb_exchange_token",
			"client_id": app_id,
			"client_secret": app_secret,
			"fb_exchange_token": short_token,
		},
		"",
	)
	return (response or {}).get("access_token") or ""


def page_of_form(form_id: str) -> tuple[str | None, str | None]:
	"""The Page a form hangs off, and that Page's own token. THE page-token resolver: the crawl runs on it,
	the form listing needs it and the drift check reads it, so it is resolved once here.

	access_token is a Password field on Facebook Page (fixtures/property_setter.json), so the column holds
	a mask and only the Auth row holds the secret."""
	if not form_id:
		return None, None
	page = frappe.db.get_value("Facebook Lead Form", form_id, "page")
	if not page:
		return None, None
	return page, get_decrypted_password("Facebook Page", page, "access_token", raise_exception=False)
