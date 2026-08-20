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
and publishes to the person who asked. The socket is the fast path; `status` is the one the tab falls
back to, because a realtime event is lost whenever the tab was reconnecting or socketio is down.

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
from frappe.utils import add_to_date, cint, now_datetime

DOCTYPE = "CRM Export Job"

# Realtime events, spelled once so the worker and the SPA cannot drift; all three are `user=`-targeted.
EVENT_PROGRESS = "crm_export_progress"
EVENT_READY = "crm_export_ready"
EVENT_FAILED = "crm_export_failed"

# How many recent jobs `mine` answers with. A tab is catching up on its own exports, not browsing history.
_RECENT = 10

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
	doc = frappe.get_doc(DOCTYPE, job)
	doc.db_set({"status": "Started", "job_id": _job_id()}, update_modified=False)
	frappe.db.commit()  # the tab is watching this row's status; a drain that runs for a minute must not hide it
	try:
		_drain(doc)
	except Exception:
		_fail(doc, frappe.get_traceback(with_context=True))


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

	doc.reload()  # stale after the attach; `Prepared Report` reloads before recording a result for this reason
	doc.db_set({"status": "Completed", "row_count": cint(made.get("rows")),
	            "truncated": cint(made.get("truncated"))}, update_modified=False)
	frappe.db.commit()
	_publish(EVENT_READY, doc, _result(doc))


def _progress(doc):
	"""The callback a producer calls as it drains. Rows so far, nothing else — a producer that cannot say
	how many rows it will end with should not be made to guess a percentage."""
	def publish(rows_so_far):
		_publish(EVENT_PROGRESS, doc, {"rows": rows_so_far})

	return publish


def _publish(event, doc, payload):
	frappe.publish_realtime(
		event,
		{"job": doc.name, "source": doc.source, "reference": doc.reference, **payload},
		user=doc.owner,
	)


def _fail(doc, error):
	"""The tab must hear about a failure, so this runs on a connection the failed drain may have dirtied:
	roll back first, then record and publish. The traceback is logged, never published — it is for the
	operator, and a stack trace is not something to put on a rep's screen."""
	frappe.db.rollback()
	doc.reload()  # same staleness as the success path: whatever failed may already have touched this row
	doc.db_set({"status": "Error", "error_message": error}, update_modified=False)
	frappe.db.commit()
	frappe.log_error(f"export failed: {doc.source} / {doc.reference}", error)
	_publish(EVENT_FAILED, doc, _result(doc))


def _job_id():
	"""The RQ job currently executing, so deleting a queued export can stop it. None outside a worker."""
	from rq import get_current_job

	job = get_current_job()
	return job.id if job else None


@frappe.whitelist()
def status(job):
	"""Where one export has got to, and its file once there is one.

	THE SOCKET IS AN OPTIMISATION, NOT THE DELIVERY. A realtime event is lost whenever the tab was
	reconnecting, the laptop slept, or socketio itself is down — and an export that silently never arrives
	is the failure this whole change exists to remove. So the tab also polls this, slowly, and whichever
	answers first wins. Reading the job row IS the gate: the doctype is `if_owner`, so another rep's job
	is not readable and this cannot hand over a file the caller did not ask for.

	The gate is ASKED, never assumed: `frappe.get_doc` does not check permissions, and relying on it to
	throw is how a rep reaches another rep's export."""
	frappe.has_permission(DOCTYPE, "read", doc=job, throw=True)
	return _result(frappe.get_doc(DOCTYPE, job))


@frappe.whitelist()
def mine(minutes=15):
	"""The caller's own recent exports — what a tab needs to catch up after it stopped listening.

	A drain outlives the surface that asked for it: a route change unmounts the list, and the realtime
	event then lands with nobody there. The job row is the record either way, so a tab coming back asks
	this and resumes — still-running jobs are tracked again, and one that finished while it was away is
	offered as a download rather than silently lost.

	Filtered on `owner` because the endpoint is `mine`. The doctype's own `if_owner` already refuses
	another rep's row; this is about what the question MEANS, since an operator's `if_owner=0` would
	otherwise answer with everybody's exports."""
	since = add_to_date(now_datetime(), minutes=-cint(minutes))
	names = frappe.get_list(
		DOCTYPE,
		filters={"owner": frappe.session.user, "creation": [">", since]},
		pluck="name",
		order_by="creation desc",
		limit=_RECENT,
	)
	return [_result(frappe.get_doc(DOCTYPE, name)) for name in names]


def _result(doc):
	"""What an export IS, once or twice asked: the ready event publishes it and the poll returns it, so
	the tab has one completion path and the two can never describe the same job differently."""
	out = {"job": doc.name, "status": doc.status, "rows": doc.row_count, "truncated": bool(doc.truncated)}
	if doc.status == "Completed":
		file = frappe.db.get_value(
			"File", {"attached_to_doctype": DOCTYPE, "attached_to_name": doc.name},
			["file_url", "file_name"], as_dict=True,
		)
		out.update(file_url=file and _saveable(file.file_url), file_name=file and file.file_name)
	elif doc.status == "Error":
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
