# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Intake guards — ONLY the public-form checks Frappe does NOT do natively. All server-side;
no client/DOM code anywhere.

Native already enforces, on every File: size (`System Settings.max_file_size`), the extension
allowlist (`allowed_file_extensions`), unsafe-PDF (`File.check_content`), and — through
`FileOverride.before_insert` — the privacy checkpoint and the shared file screener, for every channel
including this one; and on every web-form submit a per-IP rate limit (`@rate_limit` on `accept`,
10/min). Intake keeps NO file hook of its own: the screener resolves the intake channel itself
(`storage.file_screening._channel`). We add only the remaining gap:

  * throttle_intake — stricter per-IP + per-phone rate limits on the enrolment submit
                      (before_request), gated by `Intake::RateLimit::enforcement`.

(There is no captcha: Frappe web forms have no native captcha, and adding one to a
business-built form would require client DOM injection — disallowed. Bot defence is the
native per-IP limit + the stricter limits here.)

Config (rate caps) lives in the `CRM Intake Settings` Single; blanks fall back to DEFAULTS.
"""
import re

import frappe
from frappe import _

from tatva_connect import automation

_ACCEPT_CMD = "frappe.website.doctype.web_form.web_form.accept"


# Blank Single fields fall back here (Invariant A.4 — no baked form values).
DEFAULTS = {
	"ip_per_hour": 20,
	"phone_per_day": 3,
}


def _settings():
	return frappe.get_cached_doc("CRM Intake Settings")


def _cfg(field):
	return _settings().get(field) or DEFAULTS.get(field)


def _int_cfg(field):
	return int(_cfg(field) or DEFAULTS[field])


def _is_enrolment_webform():
	"""accept() carries the web_form name; only act on forms whose doctype is a live intake sink — read
	straight from the ONE brain the wildcard router uses, never through a local alias of it. Lazy import
	avoids a load cycle."""
	from tatva_connect.intake.intake import _intake_doctypes

	name = frappe.form_dict.get("web_form")
	if not name:
		return False
	dt = frappe.db.get_value("Web Form", name, "doc_type")
	return bool(dt) and dt in _intake_doctypes()


# -- Rate limiting (before_request) ------------------------------------------

def throttle_intake():
	"""before_request gate (drift walks only doc_events, so no registry `backs`). Stricter
	than frappe's native per-IP 10/min on `accept`: a per-IP and a per-phone fixed-window
	counter. Fires ONLY on the enrolment web-form submit, and only when
	`Intake::RateLimit::enforcement` is on. Fail-closed (a hit throws RateLimitExceeded)."""
	if frappe.form_dict.get("cmd") != _ACCEPT_CMD:
		return
	if not automation.is_enabled("Intake::RateLimit::enforcement"):
		return
	if not _is_enrolment_webform():
		return

	_bump("ip", frappe.local.request_ip or "unknown", _int_cfg("ip_per_hour"), 3600)
	phone = _submitted_phone()
	if phone:
		_bump("phone", phone, _int_cfg("phone_per_day"), 86400)


def _bump(scope, ident, limit, window):
	"""Fixed-window counter in redis, mirroring frappe's own rate_limiter primitives."""
	key = frappe.cache.make_key(f"intake-rl:{scope}:{ident}")
	if not frappe.cache.get(key):
		frappe.cache.setex(key, window, 0)
	if frappe.cache.incrby(key, 1) > limit:
		frappe.throw(
			_("Too many enrolment submissions — please try again later."),
			exc=frappe.RateLimitExceededError,
		)


def _submitted_phone():
	"""The phone in the submit payload, digits only (the per-phone counter key)."""
	data = frappe.form_dict.get("data")
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	return re.sub(r"\D", "", (data or {}).get("phone") or "") or None
