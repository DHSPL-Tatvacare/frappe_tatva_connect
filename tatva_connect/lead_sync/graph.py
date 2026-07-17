"""Graph API calls that surface Meta's reason and never leak the token."""
import re

import frappe
from frappe.integrations.utils import make_get_request

_TOKEN_PATTERN = re.compile(r"EAA[A-Za-z0-9_\-]{10,}")
_BASE = "https://graph.facebook.com"


def api_url(endpoint: str) -> str:
	"""Build the Graph URL at the operator's chosen version, not the fork's pinned constant."""
	return f"{_BASE}/{settings().graph_api_version}/{endpoint.lstrip('/')}"


def settings():
	return frappe.get_cached_doc("CRM Facebook Settings")


def redact_tokens(text: str) -> str:
	"""Scrub tokens; get_traceback(with_context=True) dumps locals and the token is one."""
	return _TOKEN_PATTERN.sub("<redacted>", text or "")


def graph_get(what: str, url: str, params: dict) -> dict:
	"""GET Graph; on failure throw Meta's reason, not the raw error whose URL carries the token."""
	try:
		return make_get_request(url, params=params)
	except Exception:
		frappe.throw(
			_redact(_reason(what, frappe.flags.integration_request), params),
			title=frappe._("Facebook API Error"),
		)
		raise  # unreachable: frappe.throw raises


def _reason(what: str, response) -> str:
	"""Build the message from Meta's response body, which raise_for_status() discards."""
	if response is None:
		return frappe._("Facebook {0} failed before a response was received.").format(what)
	try:
		error = (response.json() or {}).get("error") or {}
	except Exception:
		error = {}
	detail = error.get("message") or (response.text or "")[:300] or frappe._("no detail returned")
	code = error.get("code")
	return frappe._("Facebook {0} failed (HTTP {1}{2}): {3}").format(
		what, response.status_code, f", error code {code}" if code else "", detail
	)


def _redact(text: str, params: dict) -> str:
	"""Meta echoes the token back in its own error text."""
	token = (params or {}).get("access_token")
	if token:
		text = text.replace(token, "<redacted>")
	return redact_tokens(text)
