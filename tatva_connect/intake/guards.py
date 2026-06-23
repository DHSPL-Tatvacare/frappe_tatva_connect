# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Intake guards — ONLY the public-form checks Frappe does NOT do natively. All server-side;
no client/DOM code anywhere.

Native already enforces, on every File: size (`System Settings.max_file_size`), the extension
allowlist (`allowed_file_extensions`), unsafe-PDF (`File.check_content`) and privacy
(`storage.file_events.apply_privacy_policy`); and on every web-form submit a per-IP rate
limit (`@rate_limit` on `accept`, 10/min). We add only the gaps:

  * guard_file      — magic-byte sniff (bytes must match the claimed extension) + a ClamAV
                      virus scan. One File `before_insert` hook scoped to the submission,
                      gated by `Intake::File::screening`.
  * throttle_intake — stricter per-IP + per-phone rate limits on the enrolment submit
                      (before_request), gated by `Intake::RateLimit::enforcement`.

(There is no captcha: Frappe web forms have no native captcha, and adding one to a
business-built form would require client DOM injection — disallowed. Bot defence is the
native per-IP limit + the stricter limits here.)

Config (caps/host) lives in the `CRM Intake Settings` Single; blanks fall back to DEFAULTS.
"""
import re

import frappe
from frappe import _

from tatva_connect import automation

_SUBMISSION = "CRM Enrolment Submission"
_ACCEPT_CMD = "frappe.website.doctype.web_form.web_form.accept"

# Blank Single fields fall back here (Invariant A.4 — no baked form values).
DEFAULTS = {
	"clamav_host": "clamav",
	"clamav_port": 3310,
	"scanner_unavailable": "block",
	"ip_per_hour": 20,
	"phone_per_day": 3,
}

# extension -> accepted leading bytes. Native already allowlists the extension; this only
# asserts the CONTENT matches it, so a renamed .html/.svg/.exe can't pose as a .pdf.
_MAGIC = {
	"pdf": [b"%PDF"],
	"png": [b"\x89PNG\r\n\x1a\n"],
	"jpg": [b"\xff\xd8\xff"],
	"jpeg": [b"\xff\xd8\xff"],
}


def _settings():
	return frappe.get_cached_doc("CRM Intake Settings")


def _cfg(field):
	return _settings().get(field) or DEFAULTS.get(field)


def _int_cfg(field):
	return int(_cfg(field) or DEFAULTS[field])


def _is_enrolment_webform():
	"""accept() carries the web_form name; only act on forms whose doctype is ours."""
	name = frappe.form_dict.get("web_form")
	return bool(name) and frappe.db.get_value("Web Form", name, "doc_type") == _SUBMISSION


# -- File screening (File before_insert) -------------------------------------

def guard_file(doc, method=None):
	"""Scoped to enrolment-submission attachments. Magic-byte sniff + ClamAV scan. Size,
	extension, unsafe-PDF and privacy are native (see module docstring) and untouched.
	Dormant until `Intake::File::screening` is enabled."""
	if doc.attached_to_doctype != _SUBMISSION:
		return
	if not automation.is_enabled("Intake::File::screening"):
		return

	raw = doc.get_content()  # in-memory content, available at before_insert (verified in core File)
	if isinstance(raw, str):
		raw = raw.encode("utf-8", "ignore")

	_sniff(doc.file_name, raw)
	_scan(raw)


def _sniff(file_name, raw):
	ext = (file_name.rsplit(".", 1)[-1] if "." in (file_name or "") else "").lower()
	sigs = _MAGIC.get(ext)
	if sigs and not any(raw.startswith(s) for s in sigs):
		frappe.throw(_("This file's contents don't match its type."), title=_("Invalid file"))


def _scan(raw):
	"""Stream the bytes to ClamAV (INSTREAM). FOUND -> reject. Scanner unreachable ->
	honour `scanner_unavailable` (block|allow); never 500 a patient."""
	import io

	try:
		import clamd

		port = int(_cfg("clamav_port") or DEFAULTS["clamav_port"])
		cd = clamd.ClamdNetworkSocket(host=_cfg("clamav_host"), port=port, timeout=30)
		result = cd.instream(io.BytesIO(raw))
	except Exception:
		frappe.log_error(title="Intake virus scan unavailable", message=frappe.get_traceback())
		if (_cfg("scanner_unavailable") or "block") == "block":
			frappe.throw(
				_("File could not be security-scanned. Please try again later."), title=_("Upload failed")
			)
		return  # allow: operator chose to accept uploads while the scanner is down
	if (result.get("stream") or (None,))[0] == "FOUND":
		frappe.throw(_("This file failed a security scan and was not accepted."), title=_("Invalid file"))


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
