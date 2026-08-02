"""Graph API calls that surface Meta's reason and never leak the token.

Transport only. Which app a call is made on behalf of — and so which Graph version its URL carries — is
the `CRM Facebook App` row's own answer (`app.api_url`), because a token belongs to exactly one app.
"""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import frappe
from frappe.integrations.utils import make_get_request, make_post_request
from frappe.utils import cint

from tatva_connect.utils import mask_secrets


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
		response = request(strip_token_from_url(url), headers=bearer(token), **payload)
	except Exception as exc:
		frappe.throw(
			_redact(_reason(what, frappe.flags.integration_request, exc), token),
			title=frappe._("Facebook API Error"),
		)
		raise  # unreachable: frappe.throw raises
	check_usage(frappe.flags.integration_request)
	return response


# Meta reports THIS app's utilisation on EVERY response, per bucket, plus the seconds it will stay shut
# when it has closed. We read that number rather than model the formulas behind it: the buckets differ
# per endpoint (leadgen on a Page token, app-level on a user token), their maths is volume-dependent, and
# Meta changes both without notice. A limiter of our own would be a second, wrong copy of a budget its
# owner already publishes on every response.
_USAGE_HEADERS = ("x-business-use-case-usage", "x-app-usage")


def usage_records(response) -> list:
	"""Every usage record on a response, flattened. `x-app-usage` is ONE object; the business header is {object_id: [record, ...]}, a list per Page — both shapes are real and a parser for one reads the other as nothing."""
	records = []
	for header in _USAGE_HEADERS:
		raw = (getattr(response, "headers", None) or {}).get(header)
		if not raw:
			continue
		# A malformed usage header is Meta's problem, and the response it rides on is still good data we already paid for — so it is skipped, never raised.
		try:
			body = frappe.parse_json(raw)
		except Exception:  # nosec B112 — deliberate: telemetry must not fail a successful fetch
			continue
		if not isinstance(body, dict):
			continue
		if body and all(isinstance(v, list) for v in body.values()):
			records += [r for group in body.values() for r in group if isinstance(r, dict)]
		else:
			records.append(body)
	return records


def check_usage(response) -> None:
	"""Stop when Meta says stop. Refusing loses nothing: every Graph read happens before any lead is folded, so a crawl that stops here has processed nothing and moved no watermark, and the caller's own rollback-log-commit puts the refusal in the Error Log.

	Utilisation below the block is NOT logged from here — a row written in the transport layer sits in whatever transaction the caller is in (log_failure and the nightly refresh both roll back first), so it could vanish without trace. Meta's App Dashboard reports the same percentages durably."""
	records = usage_records(response)
	if not records:
		return
	blocked = max((cint(r.get("estimated_time_to_regain_access")) for r in records), default=0)
	if not blocked:
		return
	frappe.throw(
		frappe._(
			"Facebook has rate-limited this app and will not answer for another {0} seconds. Nothing was "
			"fetched and nothing was lost; the crawl picks up from the same place on its next run."
		).format(blocked),
		title=frappe._("Facebook API Rate Limited"),
	)


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
