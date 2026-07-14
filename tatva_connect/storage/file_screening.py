# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Generic file screening — the ONE brain for the extension allowlist + magic-byte sniff + ClamAV virus
scan on inbound uploads, plus the per-upload verdict log (CRM File Scan Log). Channel-agnostic: it takes
bytes + context, never a File doc or a request, so every ingress calls the SAME `screen()`.

Three checks, cheapest first, one verdict: is the extension one the operator accepts, do the bytes match
the extension they claim, and is the content free of malware. The allowlist is deliberately NOT frappe's
`allowed_file_extensions` (System Settings): that one is global (it would police desk uploads to gate a
partner) and it silently returns early when there is no request (`file.py:458`), so migrations and jobs
would bypass it. One screener, one operator page, every channel.

Activated at exactly two call sites (there is NO universal File hook — internal/Desk uploads are
untouched):
  * Intake public forms  — `intake.guards.guard_file` (File before_insert, scoped to the submission)
  * Partner file API     — `api.partner_file._attach_one` (before the File is saved)

Activation is config-driven, not code-driven: `screen()` runs ONLY when the master
`Storage::File::screening` toggle is on AND the caller's `channel` is listed in
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

# Verdicts (also the CRM File Scan Log `verdict` Select options).
_CLEAN = "Clean"
_INFECTED = "Infected"
_DISALLOWED = "Type Not Allowed"
_MISMATCH = "Type Mismatch"
_UNAVAILABLE = "Scanner Unavailable"

# Blank Single fields fall back here (Invariant A.4 — no baked form values).
DEFAULTS = {"clamav_host": "clamav", "clamav_port": 3310, "scanner_unavailable": "block"}

# extension -> accepted leading bytes. Native already allowlists the extension; this only asserts
# the CONTENT matches it, so a renamed .html/.svg/.exe can't pose as a .pdf.
_MAGIC = {
	"pdf": [b"%PDF"],
	"png": [b"\x89PNG\r\n\x1a\n"],
	"jpg": [b"\xff\xd8\xff"],
	"jpeg": [b"\xff\xd8\xff"],
}


def _settings():
	return frappe.get_cached_doc(_SETTINGS)


def _cfg(field):
	return _settings().get(field) or DEFAULTS.get(field)


def _active_channels():
	"""The set of channels the operator has activated (config, not code). Empty = dormant."""
	return {r.channel for r in _settings().active_channels}


def _is_active(channel):
	"""Screening runs for `channel` only when the master switch is on AND the operator has listed
	that channel in CRM File Screening Settings. Both gates are required; either off = dormant."""
	return automation.is_enabled(TOGGLE) and channel in _active_channels()


# -- public entry ------------------------------------------------------------

def screen(*, file_name, raw, channel, source, attached_to_doctype=None, attached_to_name=None,
	web_form=None, phone=None, source_ip=None):
	"""Screen one upload's bytes: classify -> log -> act. Dormant unless the master toggle is on AND
	`channel` is an active channel (config). Logs every verdict (accepted or blocked) to CRM File
	Scan Log, then `frappe.throw`s on a block — which each caller's framework surfaces natively
	(web-form dialog / partner `_fail`)."""
	if not _is_active(channel):
		return
	if isinstance(raw, str):
		raw = raw.encode("utf-8", "ignore")

	verdict, signature = _classify(file_name, raw)
	blocked = _is_blocked(verdict)
	_log_scan(
		channel=channel, source=source, verdict=verdict, signature=signature, blocked=blocked,
		file_name=file_name, size=len(raw), attached_to_doctype=attached_to_doctype,
		attached_to_name=attached_to_name, web_form=web_form, phone=phone, source_ip=source_ip,
	)
	if blocked:
		message, title = _block_notice(verdict)
		frappe.throw(message, title=title)


# -- classify (pure, no side effects) ----------------------------------------

def _classify(file_name, raw):
	"""Decide the verdict without acting. Cheapest first: the extension the operator allows, then the
	bytes behind it, then ClamAV. Returns (verdict, signature); signature is the clamd match name for
	an infection, else ''."""
	if not _allowed(file_name):
		return _DISALLOWED, ""
	if not _sniff(file_name, raw):
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


def _extension(file_name):
	"""The claimed extension, lowercased, without the dot. '' when the name carries none."""
	return (file_name.rsplit(".", 1)[-1] if "." in (file_name or "") else "").lower()


def _allowed(file_name):
	"""True if the operator accepts this extension. The allowlist is the FIRST gate and the only one
	that can refuse a type outright: the magic-byte sniff below can prove a .pdf is really an .html, but
	it cannot refuse an .exe that genuinely is one, because there is no fingerprint to contradict.

	Blank = every extension is accepted (the dormant default — a config, never a code, decision). The
	operator fills this in before go-live; the go-live checklist names it."""
	allowed = _cfg("allowed_extensions")
	if not allowed:
		return True
	listed = {line.strip().lstrip(".").lower() for line in str(allowed).splitlines() if line.strip()}
	return _extension(file_name) in listed


def _sniff(file_name, raw):
	"""True if the leading bytes match the claimed extension (or the extension is not one we
	fingerprint). False on a mismatch — a renamed .html/.svg/.exe posing as a .pdf."""
	sigs = _MAGIC.get(_extension(file_name))
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
