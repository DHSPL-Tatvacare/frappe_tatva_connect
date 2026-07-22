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
import frappe
from frappe import _

from tatva_connect import automation
from tatva_connect.whatsapp.phone import to_e164

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


def _intake_form_for_submit():
	"""The CRM Intake Form being submitted, or None if this is not an intake web form at all.

	accept() carries the web_form name; the sink->contract map is the ONE brain the wildcard router
	already uses, never a local alias of it. Lazy import avoids a load cycle."""
	from tatva_connect.intake.intake import _intake_doctypes

	name = frappe.form_dict.get("web_form")
	if not name:
		return None
	dt = frappe.db.get_value("Web Form", name, "doc_type")
	return _intake_doctypes().get(dt) if dt else None


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
	intake_form = _intake_form_for_submit()
	if not intake_form:
		return

	_bump("ip", frappe.local.request_ip or "unknown", _int_cfg("ip_per_hour"), 3600)
	phone = _submitted_phone(intake_form)
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


def _submitted_phone(intake_form):
	"""The submitted phone in its CANONICAL form — the per-phone counter key.

	WHICH question carries it is declared by the contract (the mapping to lead -> mobile_no, the one
	`validate` insists on exactly once). Reading a question literally named `phone` was a coincidence
	that held for the first form ever built; any other name silently lost the per-phone limit.

	Canonical via `to_e164`, NOT digits-only: `9000000011` and `+91 90000 00011` are one patient, and
	a digits-only key made them two counters — the limit was evaded by retyping the number. This is
	the same canonicalisation the lead is stored and deduped under, so the counter throttles the
	person dedup would merge."""
	field = _phone_question(intake_form)
	if not field:
		return None
	data = frappe.form_dict.get("data")
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	# cstr first: an unquoted JSON number arrives as an int and the canonicaliser is a regex.
	return to_e164(frappe.cstr((data or {}).get(field) or "")) or None


def _phone_question(intake_form):
	"""The contract's question that lands on lead -> mobile_no, or None if it declares none."""
	cfg = frappe.get_cached_doc("CRM Intake Form", intake_form)
	for m in cfg.mappings:
		if (m.target_table or "").strip() == "lead" and (m.target_field or "").strip() == "mobile_no":
			return (m.source_field or "").strip() or None
	return None
