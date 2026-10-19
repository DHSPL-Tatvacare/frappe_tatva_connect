# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE export seam: a listing becomes a file in a worker, and the tab that asked is told when it is ready.

WHY THIS EXISTS. Every export in the SPA built its file inside the HTTP request. Measured on prod for
`rubina.jasmin@tatvacare.in` — Sales Manager, inside `CRM Sales Hierarchy`, two User Permissions — one
Smart View export cost ~41.7s of SQL (25 pages x 1,085ms of count plus ~14.6s of rows) on top of ~25,000
per-row permission round trips, and died on the 120s Azure Application Gateway timeout as a 504 HTML page.
A row ceiling only buys headroom; the next wide view spends it. The request is the wrong place to do this.

THE SHAPE. `queue(...)` writes a `CRM Export Job` and returns immediately. The row's `after_insert`
enqueues `run` on the long lane. `run` resolves the producer, drains it, attaches the bytes to the row
and publishes to the person who asked, on the one path `tatva_connect.job_feed` owns: every write of the
job row is published, and the Bulk Actions panel reads the rows on load.

THE PRODUCER REGISTRY IS CLOSED. `source` is a Select and this map is the only way it becomes code, so a
caller can never name a dotted path and have a worker import it. A new export surface is a row here.

PERMISSIONS ARE THE CALLER'S, NOT THE WORKER'S. `frappe.enqueue` captures `{"user": frappe.session.user}`
and `execute_job` calls `frappe.set_user(user)` (background_jobs.py:252), so a producer runs as the person
who pressed Download and its own row gate applies unchanged. NOTHING in this file or in a producer may
pass `ignore_permissions` to a read. The two `ignore_permissions` below are on the JOB ROW and the FILE,
which are this seam's own scaffolding, never the exported data.
"""
import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from tatva_connect import job_feed

DOCTYPE = "CRM Export Job"

# What frappe's own `max_report_rows` field defaults to, for a site that has never set it.
DEFAULT_ROW_CAP = 100_000


def row_cap():
	"""The operator's ceiling on ONE export, read from frappe's OWN System Settings field.

	`max_report_rows` is declared by frappe and enforced NOWHERE on the server — its only reader in the whole
	framework is one line of report-viewer JavaScript (`query_report.js:1075`). The field exists, an operator
	knows where it lives and it says what it means, so honouring it invents no setting and hardcodes no number.

	It lives on the SEAM, not in one producer: every export this app queues answers to one operator ceiling,
	and a producer holding its own number is how the Smart View download stopped at 5,000 while the list
	download on the same site ran to 100,000."""
	return frappe.cint(frappe.get_system_settings("max_report_rows")) or DEFAULT_ROW_CAP


# source -> producer; returns {"stem", "ext", "content", "rows", "truncated"}. Closed by construction.
_PRODUCERS = {
	"Smart View": "tatva_connect.smartview.api.produce_export",
	"List": "tatva_connect.api.list_export.produce_export",
}


def queue(source, reference, fmt, params):
	"""Record the request and hand it to a worker. The CALLER has already applied its own gates — this
	seam does not know what "may export a Smart View" means and must not invent a second opinion."""
	if source not in _PRODUCERS:
		frappe.throw(_("Unknown export source {0}.").format(source))
	job = frappe.get_doc({
		"doctype": DOCTYPE,
		"source": source,
		"reference": reference,
		"fmt": fmt,
		"params": frappe.as_json(params or {}),
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — the seam's own row, gated by the caller; owner is the requester
	return {"job": job.name, "status": job.status}


def run(job):
	"""The worker. Runs as the person who asked (see the module docstring), so every read inside the
	producer is theirs."""
	if not (doc := job_feed.start("export", job)):
		return  # stopped before a worker reached it
	try:
		_drain(doc)
	except Exception:
		job_feed.fail("export", doc, f"export failed: {doc.source} / {doc.reference}")  # logs the traceback


def _drain(doc):
	producer = frappe.get_attr(_PRODUCERS[doc.source])
	params = frappe.parse_json(doc.params) if doc.params else {}
	made = producer(doc, params, _progress(doc))

	frappe.get_doc({
		"doctype": "File",
		"file_name": "{}-{}.{}".format(
			frappe.scrub(made.get("stem") or "export"),
			now_datetime().strftime("%Y%m%d-%H%M%S"),
			made.get("ext") or doc.fmt,
		),
		"attached_to_doctype": DOCTYPE,
		"attached_to_name": doc.name,
		"content": made["content"],
		# `is_private` is NOT set here: `file_events.apply_privacy_policy` derives it. The caller never decides.
	}).save(ignore_permissions=True)  # authz-ok: tier-a — the seam's own artefact; the job row above is its M1 owner and its gate
	frappe.db.commit()  # the attach dirties this row; without ending the transaction the next write is a 1020
	job_feed.finish("export", doc, {"row_count": cint(made.get("rows")), "truncated": cint(made.get("truncated"))})


def _progress(doc):
	"""The callback a producer calls as it drains. Rows so far, nothing else — a producer that cannot say
	how many rows it will end with should not be made to guess a percentage."""
	report = job_feed.progress("export", doc)
	return lambda rows_so_far: report({"rows": cint(rows_so_far)})


def _result(doc):
	"""What an export IS: the feed lists it and every event publishes it, so the two can never describe it differently."""
	status = "Error" if job_feed.abandoned(DOCTYPE, doc) else doc.status
	out = {"job": doc.name, "status": status, "rows": doc.row_count, "truncated": bool(doc.truncated),
	       "creation": doc.creation, "modified": doc.modified, "source": doc.source, "reference": doc.reference,
	       "fmt": doc.fmt, "abortable": doc.status in job_feed.LIVE,
	       "aborted": job_feed.stopped(doc)}
	if status == "Completed":
		file = frappe.db.get_value(
			"File", {"attached_to_doctype": DOCTYPE, "attached_to_name": doc.name},
			["file_url", "file_name"], as_dict=True,
		)
		out.update(file_url=file and _saveable(file.file_url), file_name=file and file.file_name)
	elif out["aborted"]:
		out["error"] = _("Stopped by you.")
	elif status == "Error":
		out["error"] = _("The export could not be prepared.")  # the traceback stays with the operator
	return out


def _saveable(file_url):
	"""The SAVE flavour of an offloaded file's URL, which an export always wants.

	An offloaded File's stored `file_url` is a proxy that REDIRECTS to Azure, and the HTML `download`
	attribute is same-origin-only — so it is silently ignored the moment the hop leaves this origin, and
	the tab gets a navigation instead of a file. `download_url(..., attachment=True)` signs a
	`Content-Disposition: attachment` onto the SAS link, which `blob_store` states is the only way a
	browser saves a blob rather than opening it. That flag is deliberately never stored on the File row
	— it would orphan the row from `by_blob_key` — so it is added here, by the control doing the download.

	A file still on local disk is same-origin already and is returned untouched."""
	from tatva_connect.storage import blob_store

	blob_key = blob_store.blob_key_from_url(file_url)
	return blob_store.download_url(blob_key, attachment=True) if blob_key else file_url
