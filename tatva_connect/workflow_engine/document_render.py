# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The deferred render behind `Generate Document` (W13, plan §4.3) — an authored `Web Template` becomes a
PDF, the PDF becomes a file the campaign document OWNS, and the journey parked on the node is told it is
there. Nothing here is a new mechanism: it is `sends._deliver_voice`'s shape, the file layer's M1, and the
same `deliver_signal` door a completed task already wakes a journey through.

DISPATCH, THEN PARK — AND WHY THE JOB IS NOT WHERE THE EDGE IS DECIDED
---------------------------------------------------------------------
The node answers `queued` or `failed` SYNCHRONOUSLY and parks; this function runs later, in its own
background job, long after that edge was taken and committed. So nothing here can change which edge the
journey left by, exactly as `_deliver_voice` argues for a dial that has already been handed to a provider.
What this reports is the OUTCOME — `document.ready` or `document.failed` — the thing a Wait downstream
listens for. A job that never reports at all is not this function's problem to solve either: the Wait's own
timeout leg is the declared backstop, and inventing a second rescue here would be a second decider over it.

A FAILED RENDER MUST NOT TAKE THE JOURNEY DOWN
----------------------------------------------
This is the one place the voice precedent is deliberately NOT copied. `_deliver_voice` raises on a refusal
because a dial has no waitable failure outcome to report and the RQ failed registry is the honest home for
it. Generate Document DECLARES `document.failed`, so a render that could not happen has somewhere truthful
to go: the row records the reason, the signal carries it, and the author's own failure leg runs. Raising
instead would park the journey for ever behind a Wait nothing can now satisfy.

The reason is written and COMMITTED before the signal is delivered — the pattern `observability/capture.py`
proved. `deliver_signal` is permission-gated and can throw; without the commit, a throw there would roll the
status write back and leave the row sitting in `Queued` with the failure recorded nowhere at all.

POINTERS, NEVER A URL (I5, plan D5)
-----------------------------------
The payload carries the `CRM Campaign Document` name and the `File` name. It does NOT carry a URL. A
journey parks — for minutes or for days — and a URL captured into journey state is a value that was true
once; the file's real address is derived at SEND time, from the File row, by the node that needs it. This
is the same rule that keeps a derived table storing pointers rather than content.

THE FILE IS BORN OWNED (M1) AND ITS PRIVACY IS NOT DECIDED HERE (M2/M3)
----------------------------------------------------------------------
The PDF is filed against the `CRM Campaign Document` row through `storage.file_manager.save`, the one file
front door, which names the parent on the insert. From there the file layer does the rest by itself: the
privacy floor is `file_events.may_be_public()` reading the operator's allowlist, and the Azure offload is
the File doc_events. There is no Azure call in this module, no `is_private`, and no kwarg asking for one —
a caller that could ask would be the second privacy decider the file rules exist to prevent.

THE TIMEOUT, AND WHY IT IS THE JOB'S OWN (plan D10, §2.3)
---------------------------------------------------------
pdfkit hands wkhtmltopdf a pipe and waits on it with NO timeout of its own, and wkhtmltopdf fetches every
remote `<img src>` while it renders — so one dead asset host holds this worker for ever. The bound is
`RENDER_TIMEOUT_SECONDS`, declared below and NAMED BY THE HANDLER AT THE ENQUEUE (`timeout=`), because the
job's own death penalty is the framework's mechanism for exactly this and it really fires: it interrupts
the blocked read and the exception lands in the `except` below, which turns it into `document.failed` with
a reason a human can read rather than a job dying silently on the failed registry.

Two other mechanisms were considered and rejected, and the reasons are recorded so they are not re-tried.
A worker THREAD cannot be used: `frappe.local` is thread-local, so `get_pdf` would resolve no site. Our own
`signal.alarm` cannot be used either: RQ's death penalty is already on SIGALRM, so a second handler would
silently disarm the job timeout — one bound, one owner. Neither the job timeout nor anything else reaps the
orphaned wkhtmltopdf child; the bound here is on OUR worker, and the child dies on its own socket timeout.
Which is the real reason plan D10 embeds template assets as data URIs: the render should never reach the
network at all, and this is only the floor under that.
"""
import frappe

from tatva_connect import automation

CAMPAIGN_DOCUMENT_DT = "CRM Campaign Document"

# The switch behind the render (D9), ships OFF, and read by the node HANDLER — a dormant bench dispatches nothing, so this job never sees one.
RENDER_SWITCH = "Document::Generation::render"

# The two outcomes a render reports — declared beside the code that CHOOSES them and read by `actions.VERBS`, the `sends.SENT`/`FAILED` arrangement, so a Wait's list and this job's delivery cannot drift.
DOCUMENT_READY = "document.ready"
DOCUMENT_FAILED = "document.failed"

# The three states the row moves through — `QUEUED` is the handler's, the other two this job's; the doctype's Select options must read exactly these and a test must lock the pair.
QUEUED = "Queued"
READY = "Ready"
FAILED = "Failed"

# Seconds. Only ever fires on a hang — the render is ~0.5s warm and ~3s on a worker's first. See the module docstring for who arms it.
RENDER_TIMEOUT_SECONDS = 120


def render_enabled() -> bool:
	return automation.is_enabled(RENDER_SWITCH)


def render_document(campaign_document, subject_doctype, subject_name, values=None, correlation=None,
                    file_name=None):
	"""Render the campaign document's template to a PDF, file it on the row, and report the outcome.

	`correlation` is the engine's token for the node that dispatched this (`journey::node`), so the signal
	wakes THAT journey and no other — a lead can be in two journeys, and correlating on the lead alone is
	the defect every other wake path in this app already avoids.

	`values` is the author's `document_values` map, already resolved against journey state by the handler.
	It is rendered through the `Web Template`'s OWN `render` — never `frappe.render_template` called here.
	The doctype owns how it renders (a Page template wraps itself in a layout, a Section does not), and a
	second renderer would be a second answer to a question the doctype has already answered.
	"""
	if not frappe.db.exists(CAMPAIGN_DOCUMENT_DT, campaign_document):
		return  # the row went with its lead, so the journey is already stopped and there is nothing to tell

	row = frappe.get_doc(CAMPAIGN_DOCUMENT_DT, campaign_document)
	try:
		if not row.template:
			raise ValueError("this campaign document names no Web Template")
		html = frappe.get_doc("Web Template", row.template).render(_values(values))
		attachment = _file_the_pdf(row, _render_pdf(html), _pdf_name(campaign_document, file_name))
		row.db_set({"status": READY, "document_file": attachment.name, "error": None})
	except Exception:
		reason = _record_failure(campaign_document, frappe.get_traceback())
		_announce(DOCUMENT_FAILED, subject_doctype, subject_name, correlation,
		          {"campaign_document": campaign_document, "reason": reason})
		return

	# Azure has no transaction, so the row that NAMES the blob is committed before anything else may raise — a rollback here would strand a real file no record points at.
	frappe.db.commit()
	_announce(DOCUMENT_READY, subject_doctype, subject_name, correlation, {
		# POINTERS. A URL here would be stale by the time the parked journey read it — see the docstring.
		"campaign_document": campaign_document,
		"document_file": attachment.name,
	})


def _values(values):
	"""The template's inputs as a dict. Parsed once, here, so a job argument that crossed the wire as JSON
	text and one handed over in-process reach `render` as the same thing."""
	if isinstance(values, str):
		values = frappe.parse_json(values) if values else None
	return values or {}


def _render_pdf(html):
	"""The HTML as PDF bytes, through frappe's own engine. THE one seam that touches the PDF toolchain.

	Imported here rather than at module load: `actions` imports this module for its constants and the web
	workers have no use for pdfkit's import cost. The bound on this call is the job's, not this function's —
	the module docstring says why, and why the two obvious alternatives are wrong."""
	from frappe.utils.pdf import get_pdf

	return get_pdf(html)


def _pdf_name(campaign_document, file_name):
	"""The name the patient sees on the attachment. The author's, if they gave one; the row's name otherwise.

	Separators are stripped because this string becomes a storage key, and it is the only free text on this
	path that the author types. `.pdf` is appended rather than assumed: a document that downloads with no
	extension opens in nothing."""
	stem = frappe.utils.cstr(file_name).replace("/", " ").replace("\\", " ").strip() or campaign_document
	return stem if stem.lower().endswith(".pdf") else f"{stem}.pdf"


def _file_the_pdf(row, content, filename):
	"""File the bytes ON the campaign document — M1, and the whole reason that doctype exists (plan D4).

	`file_manager.save` is the one file front door; naming the parent at the insert is what makes the blob's
	life exactly this row's life. Privacy is not passed and cannot be: `may_be_public()` classifies the file
	from the OWNING doctype's place on the operator allowlist, which is precisely why the PDF is owned here
	and not on the lead — putting it on `CRM Lead` would have meant allowlisting every lead attachment,
	clinical files included."""
	from tatva_connect.storage import file_manager

	return file_manager.save(
		content, filename=filename, attached_to_doctype=row.doctype, attached_to_name=row.name,
	)


def _record_failure(campaign_document, traceback):
	"""Write why the render did not happen and return that reason, phrased for a human.

	Rolled back first: a render that raised part-way may have left writes behind, and the failure record
	must not carry them. Committed immediately after, because `deliver_signal` is permission-gated and can
	throw — without the commit, a throw there would take this row's only account of the failure with it."""
	frappe.db.rollback()
	frappe.log_error(title="workflow: document render failed",
	                 message=f"campaign_document={campaign_document}\n{traceback}")
	reason = _readable(traceback)
	frappe.db.set_value(CAMPAIGN_DOCUMENT_DT, campaign_document,
	                    {"status": FAILED, "error": reason}, update_modified=False)
	frappe.db.commit()
	return reason


def _readable(traceback):
	"""The last line of the traceback — the exception and its message, which is the part an operator can act
	on. The whole trace is already in the Error Log for whoever needs it."""
	lines = [line.strip() for line in (traceback or "").strip().splitlines() if line.strip()]
	return lines[-1] if lines else "the render failed with no reason reported"


def _announce(signal_name, subject_doctype, subject_name, correlation, payload):
	"""Tell the parked journey. `deliver_signal` is THE door — the same one a completed task and a terminal
	call already wake a journey through — so an inbox row is durable whether or not the resume job survives.

	Imported inside the function: `signals` reaches `interpreter`, which imports `actions`, which imports
	this module for its constants — a module-level import would close that circle at load time."""
	from tatva_connect.workflow_engine import signals

	signals.deliver_signal(subject_doctype, subject_name, signal_name, correlation=correlation, payload=payload)
