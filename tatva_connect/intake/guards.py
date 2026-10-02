# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Intake guards — ONLY the public-form checks Frappe does NOT do natively. All server-side;
no client/DOM code anywhere.

Native already enforces, on every File: size (`System Settings.max_file_size`), the extension
allowlist (`allowed_file_extensions`), unsafe-PDF (`File.check_content`), and — through
`FileOverride.before_insert` — the privacy checkpoint and the shared file screener, for every channel
including this one; and on every web-form submit a per-IP-per-minute rate limit (`@rate_limit` on
whichever `accept` runs — payments replaces frappe's). Intake keeps NO file hook of its own: the screener resolves the intake channel itself
(`storage.file_screening._channel`). We add only the remaining gap:

  * accept          — an override of the web-form submit that spends stricter per-IP + per-phone
                      limits first (throttle_intake), gated by `Intake::RateLimit::enforcement`,
                      then runs the submit it replaced unchanged.
  * throttle_existing_check — the same per-IP limit on the public already-enrolled check that
                      `api.check_existing_patient` answers for the form's phone field, and, on its
                      own key, on the dropdown narrowing `api.link_options` answers.
  * upload_file     — a guest doorman over frappe's ONE guest-reachable File creator, which also
                      carries the per-IP upload rate limit (before_request cannot see the
                      cmd of an /api/method call — the same reason `accept` is an override). Frappe
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
from frappe.model import attachment_fieldtypes
from frappe.utils import add_to_date, cint, format_duration, now_datetime, strip_html

from tatva_connect import automation
from tatva_connect.utils import spend_rate_limit
from tatva_connect.whatsapp.phone import to_e164

_ACCEPT_CMD = "frappe.website.doctype.web_form.web_form.accept"
_SELF_ACCEPT = f"{__name__}.accept"
_RATE_SWITCH = "Intake::RateLimit::enforcement"
_HANDLE_COOKIE = "intake_upload_handle"
_HANDLE_TTL = 1800  # 30 min — the orphan window; the phase-2 reaper uses the same bound.


# Blank Single fields fall back here (Invariant A.4 — no baked form values).
DEFAULTS = {
	"ip_per_hour": 20,
	"phone_per_day": 3,
	"checks_per_hour": 40,  # an honest fill asks each question at most twice: 2 x the 20 submits an hour, and every probe beyond is enumeration
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


# -- Rate limiting (the submit override) --------------------------------------

def _replaced_accept():
	"""The submit this override replaced: the override hooked before ours (payments forks it), else frappe's own."""
	chain = [m for m in frappe.get_hooks("override_whitelisted_methods", {}).get(_ACCEPT_CMD, []) if m != _SELF_ACCEPT]
	return chain[-1] if chain else _ACCEPT_CMD


@frappe.whitelist(allow_guest=True, methods=["POST", "PUT"])  # guest-ok: override of frappe's own allow_guest web-form submit — never widens it; spends intake's limits first, then runs the replaced submit with its own gates (publish, login, edit rights) verbatim
def accept(**kwargs):
	"""Frappe's limits live on the call (`@rate_limit` on the method, keyed by the cmd at call time), so intake's do too.

	A before_request gate cannot see this call: the page posts to /api/method/<path>, and frappe sets
	form_dict.cmd only in api.handle, after before_request has run. `frappe.call` hands the replaced
	submit exactly the arguments its signature takes, as frappe's own dispatcher does."""
	throttle_intake()
	return frappe.call(_replaced_accept(), **kwargs)


def throttle_intake():
	"""Stricter than the submit's own per-IP-per-minute limit: a per-IP and a per-phone fixed-window counter.
	Fires ONLY for an intake form's submit, and only when `Intake::RateLimit::enforcement` is on.
	Also rejects an expired/missing attachment before the record is created (phase 2 §4.4)."""
	if not automation.is_enabled(_RATE_SWITCH):
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
		if df.fieldtype in attachment_fieldtypes:
			url = data.get(df.fieldname)
			if url and not frappe.db.exists("File", {"file_url": url}):
				frappe.throw(_("Your attachment expired, please attach it again."))


def _bump(scope, ident, limit, window):
	"""One intake counter, through the app's one identity-keyed limiter. Holds only what is intake's.

	417, not the limiter's own 429: frappe's uploader reads the server message on 403/417 alone, so a
	429 reaches the visitor as "the file might be corrupted". The wait is the time left in the window."""
	spend_rate_limit(
		f"intake-rl:{scope}", ident, limit, window,
		_("Too many attempts — please try again in {wait}."),
		exc=frappe.ValidationError,
	)


def throttle_existing_check(scope="check-ip"):
	"""Per-IP ceiling on the public already-enrolled check (`api.check_existing_patient`), and on any other public question a caller names its own `scope` for.

	The THIRD user of this module's one limiter, alongside the submit throttle and the upload doorman —
	same `_bump`, same switch, same `CRM Intake Settings` cap. It lives here rather than at the endpoint
	so intake keeps ONE limiting mechanism; frappe's own `@rate_limit` decorator would have been a
	second one in a module that already has an answer.

	Its own key, not the submit counter's: filling one form asks several times, and spending the
	submission budget on questions would refuse the enrolment itself. The dropdown narrowing
	(`api.link_options`) passes its own key for the same reason: one form's questions never spend another's budget.

	The CALLER spends this before it inspects anything — see `check_existing_patient`. Counting only
	once the number parsed left a caller sending junk with no ceiling at all, which is the ceiling that
	matters on an anonymous door."""
	if not automation.is_enabled(_RATE_SWITCH):
		return
	_bump(scope, frappe.local.request_ip or "unknown", _int_cfg("checks_per_hour"), 3600)


def _submitted_phone(intake_form):
	"""The submitted phone in its CANONICAL form — the per-phone counter key.

	WHICH question carries it is declared by the contract (the mapping to lead -> mobile_no, the one
	`validate` insists on exactly once). Reading a question literally named `phone` was a coincidence
	that held for the first form ever built; any other name silently lost the per-phone limit.

	Canonical via `to_e164`, NOT digits-only: `9000000011` and `+91 90000 00011` are one patient, and
	a digits-only key made them two counters — the limit was evaded by retyping the number. This is
	the same canonicalisation the lead is stored and deduped under, so the counter throttles the
	person dedup would merge."""
	field = phone_question(frappe.get_cached_doc("CRM Intake Form", intake_form))
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
			title="intake: per-phone throttle skipped, unparseable phone",
			message=f"intake_form={intake_form} question={field}",
		)
		return None


def phone_question(cfg):
	"""The contract's phone question (lead -> mobile_no, or its layer's own), or None if it declares none.

	Takes the CONTRACT DOC, not its name: the builder calls this from inside the contract's own
	`on_update`, where a re-fetch can still answer with the pre-save mappings.

	Public because it has two readers: the per-phone submit throttle here, and the builder, which
	needs the same question to bind the duplicate warning to. Which field carries the phone is the
	contract's to declare — the one `validate` insists on exactly once — never a naming convention."""
	from tatva_connect.intake.layers import phone_of, question_name, target_pair

	phone = phone_of(cfg)
	for m in cfg.mappings:
		if target_pair(m) == phone:
			return question_name(m) or None
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

	if frappe.session.user != "Guest" or not automation.is_enabled(_RATE_SWITCH):
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
		frappe.throw(_("The upload limit for this form has been reached — please try again in {0}.").format(format_duration(max(frappe.cache.ttl(frappe.cache.make_key(key)), 1))))
	result = _native()
	new_url = getattr(result, "file_url", None)
	if new_url:
		urls.append(new_url)
		frappe.cache().set_value(key, urls, expires_in_sec=_HANDLE_TTL)
	return result


# -- Visitor error stream (after_request) --------------------------------------

_UPLOAD_CMDS = ("upload_file", "frappe.handler.upload_file")
# Never logged: the session token, and an upload's raw bytes.
_UNLOGGED_KEYS = ("cmd", "csrf_token", "filedata")


def _public_cmds():
	"""Every call an intake form's page makes: the submit, the upload, and the two questions it asks while being filled."""
	from tatva_connect.intake import api

	return {_ACCEPT_CMD, *_UPLOAD_CMDS, *(f"{fn.__module__}.{fn.__name__}" for fn in (api.check_existing_patient, api.link_options))}


def log_refusal(response=None, request=None):
	"""after_request: every refusal a visitor meets on an intake form becomes one Error Log row, written after the request's own rollback."""
	from tatva_connect.intake.intake import INTAKE_SWITCH

	status = cint(getattr(response, "status_code", 0))
	if not 400 <= status < 500:
		return  # frappe logs every 5xx itself
	cmd = frappe.form_dict.get("cmd")
	if cmd not in _public_cmds() or not automation.is_enabled(INTAKE_SWITCH):
		return
	if (cmd == _ACCEPT_CMD and not _submit_sink()) or (cmd in _UPLOAD_CMDS and frappe.session.user != "Guest"):
		return
	web_form = frappe.form_dict.get("web_form")
	reason = frappe.local.response.get("exc_type") or status
	frappe.log_error(
		title=f"intake: guest refused ({reason})",
		message=frappe.as_json({
			"status": status,
			"call": cmd,
			"ip": frappe.local.request_ip,
			"told": [strip_html(m.get("message") or "") for m in frappe.get_message_log()],
			"sent": {k: v for k, v in frappe.form_dict.items() if k not in _UNLOGGED_KEYS},
		}),
		reference_doctype="Web Form" if web_form else None,
		reference_name=web_form,
	)
	frappe.db.commit()  # the request already rolled back; this keeps only the log row


# -- Guest-orphan reaper (scheduler_events) ----------------------------------

def reap_guest_orphans():
	"""Scheduled sweep: delete Guest-owned Files an intake visitor uploaded but never bonded to any
	record, once past the TTL. BOUNDED batch (oldest first) + per-file commit + log, so a slow Azure
	blob or a locked row can neither roll back the batch nor wedge the worker; unfinished rows drain
	on the next tick. Deletes only the File row — file_events.on_trash reclaims the blob on the last
	reference. Ships dormant (gated). Every HARD criterion is re-checked per file in _reap_one."""
	if not automation.is_enabled(_RATE_SWITCH):
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
		frappe.log_error(title="intake: guest orphan reap failed", message=f"file={name}")
