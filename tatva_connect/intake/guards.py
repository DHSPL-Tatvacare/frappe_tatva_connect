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
  * upload_file     — a guest doorman over frappe's ONE guest-reachable File creator, which also
                      carries the per-IP upload rate limit (before_request cannot see the upload
                      cmd — see throttle_intake). Frappe
                      cannot count an anonymous visitor's uploads (all guests are the literal
                      user "Guest", one session) nor tell one visitor's file from another's; we
                      give the visitor a countable handle (cookie + cache) and gate on it — a
                      per-handle upload cap (storage fill) and a check that a file being bonded
                      at submit belongs to this handle (cross-visitor theft). Native runs
                      verbatim once the gate passes; non-guest and dormant-flag callers fall
                      straight through.

(There is no captcha: Frappe web forms have no native captcha, and adding one to a
business-built form would require client DOM injection — disallowed. Bot defence is the
native per-IP limit + the stricter limits here.)

Config (rate caps) lives in the `CRM Intake Settings` Single; blanks fall back to DEFAULTS.
"""
import frappe
from frappe import _
from frappe.utils import add_to_date, format_duration, now_datetime

from tatva_connect import automation
from tatva_connect.whatsapp.phone import to_e164

_ACCEPT_CMD = "frappe.website.doctype.web_form.web_form.accept"
_HANDLE_COOKIE = "intake_upload_handle"
_HANDLE_TTL = 1800  # 30 min — the orphan window; the phase-2 reaper uses the same bound.


# Blank Single fields fall back here (Invariant A.4 — no baked form values).
DEFAULTS = {
	"ip_per_hour": 20,
	"phone_per_day": 3,
	"files_per_handle": 10,
	"uploads_per_ip_per_hour": 40,
	"reap_batch_size": 500,
}


def _settings():
	return frappe.get_cached_doc("CRM Intake Settings")


def _cfg(field):
	return _settings().get(field) or DEFAULTS.get(field)


def _int_cfg(field):
	return int(_cfg(field) or DEFAULTS[field])


def _submit_sink():
	"""The submission sink DOCTYPE for the current accept request, or None if this is not an intake
	web-form submit. accept() carries the web_form name; _intake_doctypes is the ONE brain that says
	whether its doctype is an enabled intake sink — never a local alias of it. Lazy import avoids a
	load cycle."""
	from tatva_connect.intake.intake import _intake_doctypes

	name = frappe.form_dict.get("web_form")
	if not name:
		return None
	dt = frappe.db.get_value("Web Form", name, "doc_type")
	return dt if dt and dt in _intake_doctypes() else None


def _intake_form_for_submit():
	"""The CRM Intake Form being submitted, or None. Derived from the sink via the ONE brain."""
	from tatva_connect.intake.intake import _intake_doctypes

	sink = _submit_sink()
	return _intake_doctypes().get(sink) if sink else None


# -- Rate limiting (before_request) ------------------------------------------

def throttle_intake():
	"""before_request gate (drift walks only doc_events, so no registry `backs`). Stricter
	than frappe's native per-IP 10/min on `accept`: a per-IP and a per-phone fixed-window
	counter. Fires ONLY on the enrolment web-form submit, and only when
	`Intake::RateLimit::enforcement` is on. Fail-closed (a hit throws RateLimitExceeded).

	The guest UPLOAD is throttled in the doorman (upload_file), NOT here: a /api/method/<path>
	request sets form_dict.cmd only in frappe.api.handle, which runs AFTER before_request
	(app.py:208 vs api/v1.py:39), so a cmd check would never see the upload here.

	Also rejects an expired/missing attachment before the record is created (phase 2 §4.4)."""
	if frappe.form_dict.get("cmd") != _ACCEPT_CMD:
		return
	if not automation.is_enabled("Intake::RateLimit::enforcement"):
		return
	sink = _submit_sink()
	if not sink:
		return

	_reject_stale_attachments(sink)
	_bump("ip", frappe.local.request_ip or "unknown", _int_cfg("ip_per_hour"), 3600)
	phone = _submitted_phone(_intake_form_for_submit())
	if phone:
		_bump("phone", phone, _int_cfg("phone_per_day"), 86400)


def _reject_stale_attachments(sink):
	"""Every Attach value the submit carries MUST still resolve to a File row. A visitor who
	uploaded, walked away past the TTL (the reaper deleted the orphan), then returned to submit would
	otherwise create a lead with a silently-missing attachment — worse than refusing. The form stays
	on screen (an accept failure is a JS callback, no reload), so nothing typed is lost. Pure
	existence check reading the sink's OWN Attach fields — no cookie, no hardcoded field name."""
	data = frappe.form_dict.get("data")
	if isinstance(data, str):
		data = frappe.parse_json(data) or {}
	data = data or {}
	for df in frappe.get_meta(sink).fields:
		if df.fieldtype in ("Attach", "Attach Image"):
			url = data.get(df.fieldname)
			if url and not frappe.db.exists("File", {"file_url": url}):
				frappe.throw(_("Your attachment expired, please attach it again."))


def _bump(scope, ident, limit, window):
	"""Fixed-window counter in redis, mirroring frappe's own rate_limiter primitives."""
	key = frappe.cache.make_key(f"intake-rl:{scope}:{ident}")
	if not frappe.cache.get(key):
		frappe.cache.setex(key, window, 0)
	if frappe.cache.incrby(key, 1) > limit:
		# Plain throw (417): frappe's uploader reads the server message only on 403/417, so a 429 reaches the rep as "the file might be corrupted"; the wait is formatted from `window`, never typed.
		frappe.throw(_("Too many enrolment submissions — please try again in {0}.").format(format_duration(window)))


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
	# cstr: an unquoted JSON number arrives as an int. A malformed one has no key — the SAVE refuses it, not this.
	try:
		return to_e164(frappe.cstr((data or {}).get(field) or "")) or None
	except frappe.ValidationError:
		frappe.clear_last_message()
		# The per-phone limit silently stops applying to this submit — say so, without the number.
		frappe.log_error(
			title="Intake per-phone throttle skipped: unparseable phone",
			message=f"intake_form={intake_form} question={field}",
		)
		return None


def _phone_question(intake_form):
	"""The contract's question that lands on lead -> mobile_no, or None if it declares none."""
	cfg = frappe.get_cached_doc("CRM Intake Form", intake_form)
	for m in cfg.mappings:
		if (m.target_table or "").strip() == "lead" and (m.target_field or "").strip() == "mobile_no":
			return (m.source_field or "").strip() or None
	return None


# -- Guest upload doorman (override of frappe.handler.upload_file) ------------

def _handle_key(handle):
	return f"intake-guest-handle:{handle}"


def _ensure_handle():
	"""The per-visitor countable handle: a cookie the browser returns on every upload call. Minted
	lazily on the first guest upload if absent — an anonymous visitor has no other stable id (the
	session id is the literal "Guest" for all of them). HttpOnly; TTL = the orphan window."""
	handle = frappe.request.cookies.get(_HANDLE_COOKIE) if frappe.request else None
	if not handle:
		handle = frappe.generate_hash(length=32)
		frappe.local.cookie_manager.set_cookie(_HANDLE_COOKIE, handle, httponly=True, max_age=_HANDLE_TTL)
	return handle


@frappe.whitelist(allow_guest=True, methods=["POST"])  # guest-ok: doorman over frappe's own allow_guest upload_file — never widens it; flag-off == native (guest mime/allowed-doctype + File screening/privacy floor still apply), armed only tightens: per-IP rate, per-handle cap, file_url bound to the handle that uploaded it (work-3-intake §8)
def upload_file():
	"""Doorman over frappe.handler.upload_file — the ONLY guest-reachable File creator (verified
	in work-3-intake §8). Non-guest and dormant-flag callers delegate immediately to the UNCHANGED
	native; native is imported directly, never re-dispatched, so there is no recursion (same
	contract as access/native_guards). For an armed guest we close the two gaps native leaves on an
	anonymous form: it cannot count a visitor's uploads, and it cannot tell one visitor's file from
	another's (every guest File is owned by the literal "Guest"). We gate on the handle, then run
	native verbatim — no fork, screening/privacy/naming keep happening where they already do."""
	from frappe.handler import upload_file as _native

	if frappe.session.user != "Guest" or not automation.is_enabled("Intake::RateLimit::enforcement"):
		return _native()

	# The dispatcher branch (native runs an arbitrary whitelisted method named in form_dict.method) is never a legitimate web-form upload — refuse it for a guest.
	if frappe.form_dict.get("method"):
		raise frappe.PermissionError

	# Per-IP flood control lives here, not in before_request: the cmd for /api/method/upload_file is set after before_request runs, so a cmd-keyed throttle there never sees it.
	_bump("upload-ip", frappe.local.request_ip or "unknown", _int_cfg("uploads_per_ip_per_hour"), 3600)

	key = _handle_key(_ensure_handle())
	urls = frappe.cache().get_value(key) or []

	file_url = frappe.form_dict.get("file_url")
	if file_url:
		# Call 3 bonds an already-uploaded file to the record; the url MUST be one this handle uploaded, else a visitor bonds a stranger's file to their own record (the theft the audit missed).
		if file_url not in urls:
			raise frappe.PermissionError
		return _native()

	# Call 1 (new bytes): bound the per-handle count before it lands. The real flood control is the per-IP rate limit above; this cap only stops an honest visitor's runaway form.
	if len(urls) >= _int_cfg("files_per_handle"):
		frappe.throw(_("The upload limit for this form has been reached — please try again in {0}.").format(format_duration(_HANDLE_TTL)))
	result = _native()
	new_url = getattr(result, "file_url", None)
	if new_url:
		urls.append(new_url)
		frappe.cache().set_value(key, urls, expires_in_sec=_HANDLE_TTL)
	return result


# -- Guest-orphan reaper (scheduler_events) ----------------------------------

def reap_guest_orphans():
	"""Scheduled sweep: delete Guest-owned Files an intake visitor uploaded but never bonded to any
	record, once past the TTL. BOUNDED batch (oldest first) + per-file commit + log, so a slow Azure
	blob or a locked row can neither roll back the batch nor wedge the worker; unfinished rows drain
	on the next tick. Deletes only the File row — file_events.on_trash reclaims the blob on the last
	reference. Ships dormant (gated). Every HARD criterion is re-checked per file in _reap_one."""
	if not automation.is_enabled("Intake::RateLimit::enforcement"):
		return
	cutoff = add_to_date(now_datetime(), seconds=-_HANDLE_TTL)
	names = frappe.get_all(
		"File",
		filters={
			"owner": "Guest",
			"attached_to_doctype": ["is", "not set"],
			"attached_to_name": ["is", "not set"],
			"is_folder": 0,
			"creation": ["<", cutoff],
		},
		order_by="creation asc",
		limit=_int_cfg("reap_batch_size"),
		pluck="name",
	)
	for name in names:
		_reap_one(name)


def _reap_one(name):
	"""Re-check ALL four HARD criteria at delete time (a file bonded since the scan is now off-limits)
	and delete only if every one still holds — one commit per file. Never raises: a bad row is rolled
	back and logged so the sweep continues. delete_doc still fires on_trash, which drops the blob."""
	f = frappe.db.get_value(
		"File", name, ["owner", "attached_to_doctype", "attached_to_name", "is_folder"], as_dict=True
	)
	if not f or f.owner != "Guest" or f.attached_to_doctype or f.attached_to_name or f.is_folder:
		return
	try:
		# authz-ok: tier-a — background sweep; gate is owner==Guest AND unattached, re-checked immediately above
		frappe.delete_doc("File", name, ignore_permissions=True, delete_permanently=True)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Intake guest-orphan reap failed", message=f"file={name}")
