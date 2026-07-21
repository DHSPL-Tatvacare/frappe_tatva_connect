# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Generic file screening — the ONE brain for the extension check + magic-byte sniff + ClamAV virus scan
on inbound uploads, plus the per-upload verdict log (CRM File Scan Log).

Three checks, cheapest first, one verdict: is the type one System Settings accepts, do the bytes match
the type they claim, and is the content free of malware. The bytes are read by `filetype`, the library
frappe already ships and already sniffs uploads with (`core/doctype/file/utils.py:76-80`) — a hand-written
magic-number table would be a second, staler copy of it. There is exactly ONE extension list and it is
Frappe's (`System Settings -> allowed_file_extensions`) — two lists was a second brain, and it is what let
a PDF be allowed here and refused there. One list read two ways is the same defect, so the value judged
here is core's OWN `set_file_type()` derivation, never a suffix we parsed off the filename. We own only
the DECISION to refuse bytes that contradict their name — Frappe reads them and never judges them — and
we run the list without Frappe's no-request early-out
(`file.py:458`), so jobs cannot bypass it.

ONE call site: `FileOverride.before_insert`, before core writes a byte to disk. There is no per-channel
hook and no caller names its own channel — `_channel()` below resolves it, once, from the request and
from what the file IS (a bulk payload is screened on both its submit lanes, Desk and API alike).

Activation is config-driven, not code-driven: `screen()` runs ONLY when the master
`Storage::File::screening` toggle is on AND the resolved `channel` is listed in
`CRM File Screening Settings -> Active Channels`. Turning a wired channel on/off tomorrow is a
config edit (add/remove a row), never a deploy. Everything else the screener consults — scanner
host/port/timeout, which verdicts block, how long a verdict is kept — lives on the same Single, and
every blank field falls back to DEFAULTS below. There are no baked form values: the code holds the
default, the form holds the operator's departure from it. Needs the `clamav` container + the `clamd`
dep (pyproject) for the virus scan; the byte sniff is local and needs neither.
"""
import mimetypes

import filetype
import frappe
from filetype.types.archive import Zip
from filetype.types.document import ZippedDocumentBase
from frappe import _
from frappe.monitor import get_trace_id
from frappe.utils import now_datetime

from tatva_connect import automation

TOGGLE = "Storage::File::screening"
_SETTINGS = "CRM File Screening Settings"
_SCAN_LOG = "CRM File Scan Log"

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

# The zip-container family, taken from filetype's OWN registry rather than listed here: a .docx, .xlsx,
# .pptx and their OpenDocument counterparts ARE zips, so a real one may read as its precise format or as
# the bare container, and both answers are the truth about the same bytes.
_ZIP_FAMILY = {t.MIME for t in filetype.TYPES if isinstance(t, (ZippedDocumentBase, Zip))}
_FINGERPRINTABLE = {t.MIME for t in filetype.TYPES}  # read from the library's own registry, never listed here

# The verdicts that refuse an upload (also the CRM Screened Verdict `verdict` Select options). Scanner
# Unavailable is NOT here: it is not a fact about the file but an outage of ours, so it is decided once by
# `scanner_unavailable` (which is also why it answers 503, not 400). One decider per verdict.
_BLOCKING = [_DISALLOWED, _MISMATCH, _INFECTED]

# Blank Single fields fall back here (Invariant A.4 — no baked form values).
DEFAULTS = {
	"clamav_host": "clamav",
	"clamav_port": 3310,
	"clamav_timeout": 30,
	"scanner_unavailable": "block",
	"blocking_verdicts": _BLOCKING,
	"scan_log_retention_days": 90,
}


def _settings():
	return frappe.get_cached_doc(_SETTINGS)


def _cfg(field):
	return _settings().get(field) or DEFAULTS.get(field)


def _active_channels():
	"""The set of channels the operator has activated (config, not code). Empty = dormant."""
	return {r.channel for r in _settings().active_channels}


def _blocking_verdicts():
	"""The verdicts the operator has listed as refusing an upload. Empty grid = the shipped three."""
	return [r.verdict for r in _settings().blocking_verdicts] or DEFAULTS["blocking_verdicts"]


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

	verdict, signature = _classify(file_name, file_type, raw)
	blocked = _is_blocked(verdict)
	_log_scan(
		channel=channel, source=frappe.session.user, verdict=verdict, signature=signature,
		blocked=blocked, file_name=file_name, size=len(raw),
		attached_to_doctype=attached_to_doctype, attached_to_name=attached_to_name,
		web_form=frappe.form_dict.get("web_form"), phone=_phone(channel),
		source_ip=getattr(frappe.local, "request_ip", None),
	)
	if blocked:
		exc, message, title = _block_exception(verdict, signature, file_name)
		frappe.throw(message, exc, title=title)


# -- classify (pure, no side effects) ----------------------------------------

def _classify(file_name, file_type, raw):
	"""Decide the verdict without acting. Cheapest first: the type the operator allows, then the bytes
	behind it, then ClamAV. `file_type` is core's own set_file_type() value, never a suffix we parsed.
	Returns (verdict, signature); signature is the clamd match name for an infection, else ''."""
	if not _allowed(file_type):
		return _DISALLOWED, ""
	if not _sniff(file_name, raw):
		return _MISMATCH, ""
	status, signature = _scan(raw)
	return {"clean": _CLEAN, "infected": _INFECTED, "unavailable": _UNAVAILABLE}[status], signature


def _is_blocked(verdict):
	"""Block a verdict the operator listed as blocking, or an unavailable scanner under the fail-closed
	`block` policy. Unavailable + operator policy `allow` is accepted (still logged, never thrown).

	Two fields, because they answer two different questions and each verdict has exactly one decider:
	Blocking Verdicts says what a bad FILE costs the caller, `scanner_unavailable` says what an outage of
	OURS costs them. An empty Blocking Verdicts grid is the shipped list, never 'block nothing'."""
	if verdict == _UNAVAILABLE:
		return _cfg("scanner_unavailable") == "block"
	return verdict in _blocking_verdicts()


def _block_notice(verdict):
	"""Patient/caller-facing (message, title) for a blocked upload."""
	if verdict == _DISALLOWED:
		return _("This file type is not accepted."), _("Invalid file")
	if verdict == _MISMATCH:
		return _("This file's contents don't match its type."), _("Invalid file")
	if verdict == _INFECTED:
		return _("This file failed a security scan and was not accepted."), _("Invalid file")
	return _("File could not be security-scanned. Please try again later."), _("Upload failed")


def _block_exception(verdict, signature, file_name):
	"""(exception, message, title) for a blocked upload — WHOSE fault it was decides the class.

	A disallowed type, a byte/type mismatch and an infection are all facts about the file the caller
	sent: ValidationError, which the partner contract maps to 400. A scanner we could not reach is an
	outage of OURS — the file was never judged — so it is frappe.ServiceUnavailableError, native 503
	(`http_status_code = 503`), which _base maps to the retryable `server_busy` with a Retry-After.
	Telling a partner their file was invalid because our clamd was down is a lie.

	The verdict rides on `detail`, the same way a field name rides on `fields`: frappe.throw() carries
	no structure, so the exception does, and _classify lifts it onto the error envelope — which is where
	both the partner and the request log read it from."""
	message, title = _block_notice(verdict)
	exc = frappe.ServiceUnavailableError(message) if verdict == _UNAVAILABLE else frappe.ValidationError(message)
	exc.detail = {
		"check": "file_screening", "verdict": verdict,
		"file_name": file_name, "signature": signature or None,
	}
	return exc, message, title


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


def _sniff(file_name, raw):
	"""True if what the bytes REALLY are agrees with the type the name claims. False on a mismatch — a
	renamed .exe or .png posing as a .pdf, a .pdf posing as a .docx.

	Both sides are read the frappe way and nothing is hand-rolled. What the bytes are comes from
	`filetype.match`, the same library and the same call frappe sniffs uploads with (file/utils.py:76-80).
	What the name claims comes from `mimetypes.guess_type`, the same call core's `set_file_type` makes
	(file.py:439) — the mime rather than its round-tripped extension, because the round trip renames
	image/tiff to TIF and would refuse every genuine .tif.

	The question is asked of the CLAIMED type, never of the bytes. A type filetype can fingerprint must be
	proved: a .pdf holding `<html>` is refused, because PDF has a signature and these bytes are not it.
	A type it cannot fingerprint is waved through: CSV, TXT and JSON have no signature at all, so nothing
	could ever contradict them. Asking it of the bytes instead lets an unrecognisable payload wear any
	extension it likes — filetype answers None for HTML, and a .pdf full of HTML would sail past."""
	claimed = mimetypes.guess_type(file_name)[0]
	if not claimed or claimed not in _FINGERPRINTABLE:
		return True
	kind = filetype.match(raw)
	return bool(kind) and (claimed == kind.mime or _ZIP_FAMILY >= {claimed, kind.mime})


def _scan(raw):
	"""Stream the bytes to ClamAV (INSTREAM). Returns (status, signature): ('clean', ''),
	('infected', <name>), or ('unavailable', '') when the scanner can't be reached. Never throws
	and never 500s a caller — the caller decides block/allow."""
	import io

	try:
		import clamd

		cd = clamd.ClamdNetworkSocket(
			host=_cfg("clamav_host"), port=int(_cfg("clamav_port")), timeout=int(_cfg("clamav_timeout"))
		)
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
	"""Activator for `Storage::File::screening`: register/deregister the scan log with Log Settings so its
	daily cleanup trims it at the operator's retention. Mirrors observability.capture.apply_logging.

	The enabled branch declares an end state rather than a creation: an existing row has its `days` set
	too, so re-saving the settings with a new retention is what applies it, and the toggle need not be
	cycled. Log Settings is the ONE place a retention is executed; this only registers the doctype."""
	days = int(_cfg("scan_log_retention_days"))
	settings = frappe.get_doc("Log Settings")
	row = next((r for r in settings.logs_to_clear if r.ref_doctype == _SCAN_LOG), None)
	if enabled:
		if row:
			row.days = days
		else:
			settings.append("logs_to_clear", {"ref_doctype": _SCAN_LOG, "days": days})
		settings.save(ignore_permissions=True)  # authz-ok: tier-a — scan-log row (background worker) / operator activator
	elif row:
		settings.remove(row)
		settings.save(ignore_permissions=True)  # authz-ok: tier-a — scan-log row (background worker) / operator activator
