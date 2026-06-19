"""Request-tail capture for external-facing endpoints.

Wired in hooks.py:
    before_request = ["tatva_connect.observability.capture.stamp_start"]
    after_request  = [..., "tatva_connect.observability.capture.log_request"]

`stamp_start` records a monotonic start on every request (one float, negligible).
`log_request` runs after the response is built; for the watched endpoints only it
writes ONE row into CRM API Request Log (the disposable raw tier). The rollup job
later aggregates those rows into CRM API Metric.

The watch list is built from the SAME path constant the partner-API error handler keys
off (`_PARTNER_PATH`) and from the webhook handler modules themselves — never a
hand-typed path literal, so a module rename can't silently stop logging. Detection uses
the same anchored `startswith` as `normalise_partner_response`. Telemetry must never
break a response, so the whole thing is wrapped and any failure is logged, not raised.
"""
import time

import frappe
from frappe.utils import now_datetime

from tatva_connect.api.partner import _PARTNER_PATH
from tatva_connect.telephony import handler as _telephony_handler
from tatva_connect.telephony.adapter import TELEPHONY_MEDIUM
from tatva_connect.whatsapp import webhook as _whatsapp_webhook


def _method_prefix(module):
	"""The `/api/method/<module>.` RPC path prefix for a module's whitelisted functions,
	derived from the module itself (mirrors how `_PARTNER_PATH` is shaped)."""
	return f"/api/method/{module.__name__}."


# (anchored path prefix, channel, fixed source | None = identify by the caller).
_WATCH = (
	(_PARTNER_PATH, "Partner API", None),
	(_method_prefix(_whatsapp_webhook), "Inbound Webhook", "WhatsApp"),
	(_method_prefix(_telephony_handler), "Inbound Webhook", TELEPHONY_MEDIUM),
)


def stamp_start(*args, **kwargs):
	"""before_request: stamp a monotonic start time on the request-local."""
	frappe.local._tc_obs_t0 = time.monotonic()


def log_request(response=None, request=None):
	"""after_request: log one raw row for a watched endpoint. Never raises."""
	try:
		req = request or getattr(frappe, "request", None)
		if req is None:
			return
		path = req.path or ""
		match = next(((ch, src) for prefix, ch, src in _WATCH if path.startswith(prefix)), None)
		if match is None:
			return
		channel, source = match
		if source is None:  # Partner API: identify by the authenticated caller
			source = getattr(getattr(frappe, "session", None), "user", None) or "Unknown"

		t0 = getattr(frappe.local, "_tc_obs_t0", None)
		duration_ms = int((time.monotonic() - t0) * 1000) if t0 is not None else None
		code = int(getattr(response, "status_code", 0) or 0)
		# last dotted segment of the method path, e.g. ...partner.lead_create -> lead_create
		endpoint = path.rstrip("/").rsplit("/", 1)[-1].rsplit(".", 1)[-1] or path

		frappe.get_doc({
			"doctype": "CRM API Request Log",
			"request_time": now_datetime(),
			"channel": channel,
			"endpoint": endpoint,
			"source": source,
			"http_method": getattr(req, "method", None),
			"status_code": code,
			"is_error": 1 if code >= 400 else 0,
			"duration_ms": duration_ms,
		}).insert(ignore_permissions=True)
		# Commit the log row explicitly. By the time after_request runs, Frappe has already
		# committed (success) or rolled back (error) the handler's own transaction, so the
		# only pending write here is this log row — and the framework won't commit again
		# after hooks, so without this the row would be dropped at teardown.
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "observability.log_request")
