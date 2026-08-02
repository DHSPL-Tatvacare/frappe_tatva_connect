"""The Facebook credential model, in one place.

Three token kinds matter. A SHORT user token is what Graph API Explorer hands an operator and it lasts
an hour or two. A LONG user token lasts 60 days and is minted by exchanging a short one against the app
secret. A PAGE token derived from a long user token does not expire, and it is the credential the crawl
runs on, so a lapsed user token stops nothing once bootstrap has happened.

The operator pastes the short token. Everything below that is the server's job.

A token is issued by exactly ONE app, and only that app can exchange or describe it. So every record
holding a token names its app in `facebook_app`, and `app_for` is the one resolver. A Page is NOT owned
by an app — a Business owns Pages and Apps alike — so nothing here walks from one to the other; each
record answers only for the token it holds.
"""
from datetime import datetime, timezone

import frappe
from frappe.utils.password import get_decrypted_password

from tatva_connect.lead_sync.graph import graph_get, graph_post, redact_tokens

APP = "CRM Facebook App"


def app_for(doc):
	"""THE app resolver: the app that issued the token THIS record holds.

	Not derived from a Page, a form or a Business — none of them owns an app. It is declared on the
	record and verified against Graph's own answer when the token is saved."""
	name = doc.get("facebook_app")
	if not name:
		frappe.throw(
			frappe._("{0} names no Facebook App, so there is no credential to call Facebook with.").format(
				doc.get("name") or doc.doctype
			),
			title=frappe._("Facebook App required"),
		)
	return frappe.get_cached_doc(APP, name)


def token_info(access_token: str, app) -> dict:
	"""Graph's own account of a token: type, app, expiry, scopes. THE debug_token call site, so every
	consumer reads one shape. An app token authenticates the question when the app secret is configured,
	which is also the only way Graph will describe a token that has already lapsed."""
	if not access_token:
		return {}
	response = graph_get(
		"token inspection",
		app.api_url("debug_token"),
		{"input_token": access_token},
		app.inspector() or access_token,
	)
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


def assert_issued_by(info: dict, app) -> None:
	"""Refuse a token Graph says belongs to a different app than the one this record names.

	This is the whole reason the app is declared rather than guessed. Exchanging against the wrong app
	fails, and it fails QUIETLY — Meta refuses and the operator keeps a token that dies within the hour.
	Named here, at save time, with both apps in the message."""
	issued_by = str((info or {}).get("app_id") or "")
	if not issued_by or issued_by == str(app.app_id):
		return
	frappe.throw(
		frappe._(
			"Facebook reports this token was issued by app {0} ({1}), not {2} ({3}). Pick the app the "
			"token came from, or paste a token issued by the app named here."
		).format(issued_by, (info or {}).get("application") or "unknown", app.app_id, app.app_name),
		title=frappe._("Token belongs to a different app"),
	)


def refresh_credential(source) -> None:
	"""Bring the stored token to its durable form and record when it lapses. THE save-time seam.

	Graph is inspected ONCE per save and the payload is reused, so a save costs one call rather than one
	per question asked of it. A wrong app is the ONE thing raised from here: it is an operator mistake
	with a clear correction, and swallowing it is what let a dead token pass for a working one. Anything
	else — an unaskable token, a Graph outage, a refused exchange — must not stop an operator saving a
	source, and the Validate Token report is where each of those shows up."""
	access_token = source.get_password("access_token", raise_exception=False)
	if not access_token:
		return
	try:
		app = app_for(source)
		info = token_info(access_token, app)
		assert_issued_by(info, app)
		if is_short(info):
			long_token = exchange_for_long_lived(access_token, app)
			if long_token:
				source.access_token = long_token
				info = token_info(long_token, app)
		if source.meta.has_field("token_expires_on"):
			source.token_expires_on = stops_working_on(info)
	except frappe.ValidationError:
		raise
	except Exception:
		frappe.log_error(
			title=f"Facebook credential refresh failed: {source.name or source.type}",
			message=redact_tokens(frappe.get_traceback(with_context=True)),
		)


def exchange_for_long_lived(short_token: str, app) -> str:
	"""Trade a short user token for the 60-day one. Returns "" when the app secret is not configured."""
	secret = app.secret()
	if not (short_token and app.app_id and secret):
		return ""
	# An OAuth endpoint, not a Graph read: the credentials ARE the payload and the token is the subject of
	# the request, so nothing here authorizes it and no bearer header is sent. POSTed rather than sent as
	# query params, so the App Secret never enters a URL that a proxy, an access log or a retry can echo.
	response = graph_post(
		"long-lived token exchange",
		app.api_url("oauth/access_token"),
		{
			"grant_type": "fb_exchange_token",
			"client_id": app.app_id,
			"client_secret": secret,
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
