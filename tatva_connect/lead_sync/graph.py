"""Graph API calls that surface Meta's reason and never leak the token."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import frappe
from frappe.integrations.utils import make_get_request, make_post_request

from tatva_connect.utils import mask_secrets

_BASE = "https://graph.facebook.com"


def api_url(endpoint: str) -> str:
	"""Build the Graph URL at the operator's chosen version, not the fork's pinned constant."""
	return f"{_BASE}/{settings().graph_api_version}/{endpoint.lstrip('/')}"


def settings():
	return frappe.get_cached_doc("CRM Facebook Settings")


def redact_tokens(text: str) -> str:
	"""The Facebook-side name for the shared masker in `utils`, kept so every existing caller reads one
	name. There is ONE masking rule in this app and this is not a second one."""
	return mask_secrets(text)


def graph_get(what: str, url: str, params: dict, token: str) -> dict:
	"""GET Graph with the token in the Authorization header; on failure throw Meta's reason, not the raw error.
	A query-param token is echoed into the Error Log, the access log and browser history, so it never goes there."""
	return _call(make_get_request, what, url, {"params": strip_token(params)}, token)


def graph_post(what: str, url: str, data: dict, token: str) -> dict:
	"""POST Graph with the payload in the request BODY. For the OAuth endpoints the credentials ARE the
	payload and no header can carry them, so the body is the only place they do not reach a URL."""
	return _call(make_post_request, what, url, {"data": strip_token(data)}, token)


def _call(request, what: str, url: str, payload: dict, token: str) -> dict:
	"""One transport rule for both verbs: strip any token from the URL, carry it in the header, and
	explain a failure with THIS call's response."""
	# Cleared first: make_request only sets this once a response exists, so a connection or retry failure
	# would otherwise be explained using the PREVIOUS call's response, naming the wrong status and body.
	frappe.flags.integration_request = None
	try:
		return request(strip_token_from_url(url), headers=bearer(token), **payload)
	except Exception as exc:
		frappe.throw(
			_redact(_reason(what, frappe.flags.integration_request, exc), token),
			title=frappe._("Facebook API Error"),
		)
		raise  # unreachable: frappe.throw raises


def bearer(token: str) -> dict:
	"""The one place a Facebook token becomes a request header."""
	return {"Authorization": f"Bearer {token}"} if token else {}


def strip_token(params: dict) -> dict:
	"""The header is the only carrier, so a caller's access_token param is dropped rather than trusted."""
	return {k: v for k, v in (params or {}).items() if k != "access_token"}


def strip_token_from_url(url: str) -> str:
	"""Graph's paging `next` is a complete URL carrying its own access_token in the query string."""
	split = urlsplit(url)
	if not split.query:
		return url
	kept = [(k, v) for k, v in parse_qsl(split.query, keep_blank_values=True) if k != "access_token"]
	return urlunsplit(split._replace(query=urlencode(kept)))


def _reason(what: str, response, exc: Exception) -> str:
	"""Build the message from Meta's response body, which raise_for_status() discards.

	A retried 5xx arrives here with no response at all: urllib3 exhausts the retries and raises, so the
	exception is the only account of what happened and is named rather than guessed at."""
	if response is None:
		return frappe._("Facebook {0} failed before any response was received: {1}").format(
			what, f"{type(exc).__name__}: {exc}"[:300]
		)
	try:
		error = (response.json() or {}).get("error") or {}
	except Exception:
		error = {}
	detail = error.get("message") or (response.text or "")[:300] or frappe._("no detail returned")
	code = error.get("code")
	return frappe._("Facebook {0} failed (HTTP {1}{2}): {3}").format(
		what, response.status_code, f", error code {code}" if code else "", detail
	)


def _redact(text: str, token: str) -> str:
	"""Meta echoes the token back in its own error text, and this message is thrown to the browser."""
	return mask_secrets(text, extra=(token,))
