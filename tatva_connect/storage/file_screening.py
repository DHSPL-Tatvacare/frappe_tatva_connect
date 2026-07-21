# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Generic file screening — the ONE brain for the extension check + magic-byte sniff + ClamAV virus scan
on inbound uploads, plus the per-upload verdict log (CRM File Scan Log).

Three checks, cheapest first, one verdict: is the type one System Settings accepts, do the bytes match
the type they claim, and is the content free of malware. There is exactly ONE extension list and it is
Frappe's (`System Settings -> allowed_file_extensions`) — two lists was a second brain, and it is what let
a PDF be allowed here and refused there. One list read two ways is the same defect, so the value judged
here is core's OWN `set_file_type()` derivation, never a suffix we parsed off the filename. We own only
the byte-signature check Frappe has not got, and we run the list without Frappe's no-request early-out
(`file.py:458`), so jobs cannot bypass it.

ONE call site: `FileOverride.before_insert`, before core writes a byte to disk. There is no per-channel
hook and no caller names its own channel — `_channel()` below resolves it, once, from the request and
from what the file IS (a bulk payload is screened on both its submit lanes, Desk and API alike).

Activation is config-driven, not code-driven: `screen()` runs ONLY when the master
`Storage::File::screening` toggle is on AND the resolved `channel` is listed in
`CRM File Screening Settings -> Active Channels`. Turning a wired channel on/off tomorrow is a
config edit (add/remove a row), never a deploy. Scanner config (clamd host/port, unavailable
policy) lives in the same Single; blanks fall back to DEFAULTS. Needs the `clamav` container + the
`clamd` dep (pyproject) for the virus scan; the magic-byte sniff is local and needs neither.
"""
import frappe
from frappe import _
from frappe.monitor import get_trace_id
from frappe.utils import now_datetime

from tatva_connect import automation

TOGGLE = "Storage::File::screening"
_SETTINGS = "CRM File Screening Settings"
_SCAN_LOG = "CRM File Scan Log"
_RETENTION_DAYS = 90

# Channels (also the CRM Screened Channel `channel` Select options).
_INTAKE = "Intake"
_PARTNER = "Partner API"
_BULK_JOB = "CRM Bulk Job"  # the doctype a bulk payload file is owned by, on BOTH submit lanes

# Verdicts (also the CRM File Scan Log `verdict` Select options).
_CLEAN = "Clean"
_INFECTED = "Infected"
_DISALLOWED = "Type Not Allowed"
_MISMATCH = "Type Mismatch"
_UNAVAILABLE = "Scanner Unavailable"

# Blank Single fields fall back here (Invariant A.4 — no baked form values).
DEFAULTS = {"clamav_host": "clamav", "clamav_port": 3310, "scanner_unavailable": "block"}

# file_type -> accepted leading bytes, keyed on core's set_file_type() value (the SAME derivation _allowed reads, so .jpeg and .jpg are ONE entry): native allowlists the type, this only asserts the CONTENT matches it.
_MAGIC = {
	"PDF": [b"%PDF"],
	"PNG": [b"\x89PNG\r\n\x1a\n"],
	"JPG": [b"\xff\xd8\xff"],
}


def _settings():
	return frappe.get_cached_doc(_SETTINGS)


def _cfg(field):
	return _settings().get(field) or DEFAULTS.get(field)


def _active_channels():
	"""The set of channels the operator has activated (config, not code). Empty = dormant."""
	return {r.channel for r in _settings().active_channels}


def _channel(attached_to_doctype, attached_to_name):
	"""THE channel resolver — which ingress this upload arrived on, decided ONCE from the request.

	A bulk payload is keyed on what it IS, not on who submitted it, and so is tested FIRST: submit_job is
	one path with two lanes, and the Desk lane carries no partner_ctx, is not Guest and hangs off no
	intake sink — so a request-only resolver screened the API lane and waved the Desk lane straight
	through. The one exception is `input_format == "inline"`, which is our OWN serialization of an
	already-parsed JSON body: it is never an upload and there is nothing to screen.

	Partner API is otherwise the @_api preamble's own per-request stash, so every partner endpoint answers
	the same way without naming itself. Intake is an attachment on a live submission sink, or ANY Guest
	upload — every CRM Intake Form publishes with login_required=0 and no other upload surface in the
	product is reachable by Guest, and a web-form upload arrives unattached (attach.js:80 sends no
	doctype), so the sink test alone would miss every patient upload. Anything else — Desk, internal
	jobs — is not a screened channel and returns None."""
	from tatva_connect.intake.intake import _intake_doctypes

	if attached_to_doctype == _BULK_JOB:
		return None if _is_inline_job(attached_to_name) else _PARTNER
	if getattr(frappe.local, "partner_ctx", None) is not None:
		return _PARTNER
	if attached_to_doctype in _intake_doctypes() or frappe.session.user == "Guest":
		return _INTAKE
	return None


def _is_inline_job(job_name):
	"""True for a payload the app serialized itself from an inline JSON body — never an uploaded file."""
	return frappe.db.get_value(_BULK_JOB, job_name, "input_format") == "inline"


# -- public entry ------------------------------------------------------------

def screen(*, file_name, file_type, raw, attached_to_doctype=None, attached_to_name=None):
	"""Screen one upload's bytes: master toggle -> resolve channel -> classify -> log -> act. Dormant
	unless the master toggle is on AND the resolved channel is an active channel (config). Logs every
	verdict (accepted or blocked) to CRM File Scan Log, then `frappe.throw`s on a block — which each
	ingress's framework surfaces natively (web-form dialog / partner `_fail`).

	The master toggle is read FIRST and on its own: this runs on EVERY File insert on the bench, and
	resolving the channel costs a cached-settings read plus a db lookup. Dormant must mean zero work."""
	if not automation.is_enabled(TOGGLE):
		return
	channel = _channel(attached_to_doctype, attached_to_name)
	if not channel or channel not in _active_channels():
		return
	if isinstance(raw, str):
		raw = raw.encode("utf-8", "ignore")

	verdict, signature = _classify(file_type, raw)
	blocked = _is_blocked(verdict)
	_log_scan(
		channel=channel, source=frappe.session.user, verdict=verdict, signature=signature,
		blocked=blocked, file_name=file_name, size=len(raw),
		attached_to_doctype=attached_to_doctype, attached_to_name=attached_to_name,
		web_form=frappe.form_dict.get("web_form"), phone=_phone(channel),
		source_ip=getattr(frappe.local, "request_ip", None),
	)
	if blocked:
		message, title = _block_notice(verdict)
		frappe.throw(message, title=title)


# -- classify (pure, no side effects) ----------------------------------------

def _classify(file_type, raw):
	"""Decide the verdict without acting. Cheapest first: the type the operator allows, then the bytes
	behind it, then ClamAV. `file_type` is core's own set_file_type() value, never a suffix we parsed.
	Returns (verdict, signature); signature is the clamd match name for an infection, else ''."""
	if not _allowed(file_type):
		return _DISALLOWED, ""
	if not _sniff(file_type, raw):
		return _MISMATCH, ""
	status, signature = _scan(raw)
	return {"clean": _CLEAN, "infected": _INFECTED, "unavailable": _UNAVAILABLE}[status], signature


def _is_blocked(verdict):
	"""Block a hard fail, or an unavailable scanner under the fail-closed `block` policy.
	Unavailable + operator policy `allow` is accepted (still logged, never thrown)."""
	if verdict in (_DISALLOWED, _MISMATCH, _INFECTED):
		return True
	if verdict == _UNAVAILABLE:
		return (_cfg("scanner_unavailable") or "block") == "block"
	return False


def _block_notice(verdict):
	"""Patient/caller-facing (message, title) for a blocked upload."""
	if verdict == _DISALLOWED:
		return _("This file type is not accepted."), _("Invalid file")
	if verdict == _MISMATCH:
		return _("This file's contents don't match its type."), _("Invalid file")
	if verdict == _INFECTED:
		return _("This file failed a security scan and was not accepted."), _("Invalid file")
	return _("File could not be security-scanned. Please try again later."), _("Upload failed")


def _phone(channel):
	"""The submitting patient's phone, for an intake upload only — read through the ONE parser that also
	keys the per-phone rate limit, never a second copy. No other channel carries a submit payload."""
	from tatva_connect.intake.guards import _submitted_phone

	return _submitted_phone() if channel == _INTAKE else None


def _allowed(file_type):
	"""True if System Settings accepts this type. It is the FIRST gate and the only one that can refuse a
	type outright: the magic-byte sniff below can prove a .pdf is really an .html, but it cannot refuse an
	.exe that genuinely is one, because there is no fingerprint to contradict.

	This is core's `validate_file_extension` rule (file.py:456-468), expressed against the SAME field and
	the SAME core-derived `file_type`, with exactly ONE deliberate difference: core early-returns when
	there is no `frappe.request`, so it does not police jobs, migrations or integrations, and we do. The
	comparison must stay byte-for-byte core's — no strip, no lower, no dot handling — because a second
	reading of one list is a second brain. Locked by test_file_lifecycle_seam.TestOneExtensionList.

	Blank list = every type accepted, which is Frappe's own default. No file_type means core could not
	derive one, and core allows that too. `System Settings -> Files` is the one place an operator sets it."""
	allowed = frappe.get_system_settings("allowed_file_extensions")
	if not file_type or not allowed:
		return True
	return file_type in allowed.splitlines()


def _sniff(file_type, raw):
	"""True if the leading bytes match the type core derived (or it is not one we fingerprint).
	False on a mismatch — a renamed .html/.svg/.exe posing as a .pdf."""
	sigs = _MAGIC.get(file_type)
	return not sigs or any(raw.startswith(s) for s in sigs)


def _scan(raw):
	"""Stream the bytes to ClamAV (INSTREAM). Returns (status, signature): ('clean', ''),
	('infected', <name>), or ('unavailable', '') when the scanner can't be reached. Never throws
	and never 500s a caller — the caller decides block/allow."""
	import io

	try:
		import clamd

		port = int(_cfg("clamav_port") or DEFAULTS["clamav_port"])
		cd = clamd.ClamdNetworkSocket(host=_cfg("clamav_host"), port=port, timeout=30)
		result = cd.instream(io.BytesIO(raw))
	except Exception:
		frappe.log_error(title="File virus scan unavailable", message=frappe.get_traceback())
		return "unavailable", ""
	stream = result.get("stream") or (None, None)
	if stream[0] == "FOUND":
		return "infected", stream[1] or ""
	return "clean", ""


# -- log (out-of-band, transaction-consistent) -------------------------------

def _log_scan(*, channel, source, verdict, signature, blocked, file_name, size,
	attached_to_doctype, attached_to_name, web_form, phone, source_ip):
	"""Record the verdict out-of-band, with commit semantics matched to the outcome so the log is
	never inconsistent with what actually persisted:

	  * BLOCKED  -> immediate push (`enqueue_after_commit=False`). The throw guarantees the
	    request rolls back, so an after-commit job would never fire; we push independently so the
	    blocked row survives the rollback that rejects the file.
	  * ACCEPTED -> `enqueue_after_commit=True`. The row is written ONLY if the surrounding
	    transaction commits (the file truly persisted). If a later step rolls the request back, the
	    job is dropped — never an 'Accepted' row for a file that never existed.

	Fail-safe: logging must never change the scan decision. A failure to enqueue is swallowed and
	logged, so the caller's block or accept still happens exactly as if logging were absent."""
	try:
		frappe.enqueue(
			_write_scan_log,
			queue="short",
			enqueue_after_commit=not blocked,
			scan_time=now_datetime(),
			channel=channel,
			source=source,
			verdict=verdict,
			action="Blocked" if blocked else "Accepted",
			signature=signature,
			file_name=file_name,
			file_size=size,
			attached_to_doctype=attached_to_doctype,
			attached_to_name=attached_to_name,
			web_form=web_form,
			source_ip=source_ip,
			phone=phone,
			# Read HERE, in the request. _write_scan_log runs in a worker, where get_trace_id() would
			# answer with the JOB's id — and the verdict would join to nothing.
			trace_id=get_trace_id(),
		)
	except Exception:
		frappe.log_error(title="File scan log enqueue failed", message=frappe.get_traceback())


def _write_scan_log(**fields):
	"""Worker: persist one CRM File Scan Log row in its own frappe-managed job transaction —
	commits on success, rolls back on error, so a failed log is a missing row, never a partial one."""
	frappe.get_doc(dict(doctype=_SCAN_LOG, **fields)).insert(ignore_permissions=True)  # authz-ok: tier-a — scan-log row (background worker) / operator activator


def apply_scan_logging(enabled):
	"""Activator for `Storage::File::screening`: register/deregister the scan log with Log Settings
	so its daily cleanup trims it at `_RETENTION_DAYS`. Mirrors observability.capture.apply_logging."""
	settings = frappe.get_doc("Log Settings")
	row = next((r for r in settings.logs_to_clear if r.ref_doctype == _SCAN_LOG), None)
	if enabled and not row:
		settings.append("logs_to_clear", {"ref_doctype": _SCAN_LOG, "days": _RETENTION_DAYS})
		settings.save(ignore_permissions=True)  # authz-ok: tier-a — scan-log row (background worker) / operator activator
	elif not enabled and row:
		settings.remove(row)
		settings.save(ignore_permissions=True)  # authz-ok: tier-a — scan-log row (background worker) / operator activator
