# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE bulk-list-action seam: Assign / Clear Assignment / Bulk Edit / Bulk Delete on 20+ rows move to a
worker; a selection under 20, or the whole feature switched off, runs exactly as it does today.

WHY 20, AND WHY OWNED HERE. Frappe's own dispatchers each pick a different threshold with no shared
reasoning — `bulk_update.submit_cancel_or_update_docs` enqueues past 20, `reportview.delete_items` past
10, and `assign_to.add_multiple`/`remove_multiple` never enqueue at all, regardless of size. This seam
never calls those three dispatchers — it calls the INNERMOST function each one calls
(`assign_to.add`/`remove`, `bulk_update._bulk_action`, `reportview.delete_bulk`), so ONE threshold, owned
here, applies uniformly, and frappe's own dispatcher code is simply never reached from this door.
`Bulk Delete`'s executor is not purely-frappe-only, though: it also reuses CRM-specific helper functions
(`crm.api.doc.get_linked_docs_of_document`/`remove_linked_doc_reference`) to cascade linked documents
before the terminal frappe delete, matching what the CRM app's own list-view Delete button does.

WHY A DURABLE ROW, NOT JUST A SOCKET EVENT. `frappe.publish_progress` and `frappe.msgprint(realtime=True)`
are frappe's own completion signals for exactly this class of job, and they are fire-and-forget: if the
tab is not connected at that instant — reconnecting, backgrounded, socketio down — the message is gone
and nothing durable is left to recover it. `tatva_connect.exports` solved the same problem for exports
by keeping a job row as the fallback truth and the socket as only the fast path; this seam is that same
shape, generalised to a mutation instead of a file.

THE EXECUTOR REGISTRY IS CLOSED. `action` is a Select and this map is the only way it becomes code.

PERMISSIONS ARE THE CALLER'S. `frappe.enqueue` captures `{"user": frappe.session.user}` and
`execute_job` calls `frappe.set_user(user)`, so a worker runs as the person who clicked the action and
every one of frappe's own permission checks inside `add`/`remove`/`_bulk_action`/`delete_bulk` applies
unchanged. The two `ignore_permissions=True` calls below are on the JOB ROW ITSELF, this seam's own
scaffolding — never on the mutation.
"""
import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from tatva_connect.automation import settings as automation
from tatva_connect.tasks import tasks

DOCTYPE = "CRM List Action Job"

# Below this many rows, or with the feature off, the action runs inline exactly as it does today.
THRESHOLD = 20

# Frappe's own bulk_update.py refuses above this; restoring the same ceiling here since this seam
# calls the executor directly and no longer goes through that dispatcher's own check.
MAX_ROWS = 500

# Dormant until an operator turns this on (tatva_connect/automation/seed.py ships every new row enabled=0).
AUTOMATION_KEY = "Lead::BulkActions::async"

# Realtime events, spelled once so the worker and the SPA cannot drift; all three are `user=`-targeted.
EVENT_PROGRESS = "crm_bulk_progress"
EVENT_READY = "crm_bulk_ready"
EVENT_FAILED = "crm_bulk_failed"

# How many recent jobs `mine` answers with. A tab is catching up on its own jobs, not browsing history.
_RECENT = 10

# action -> executor; each returns {"total", "succeeded", "failed", "failed_names"}. Closed by construction.
_EXECUTORS = {
	"Assign": "tatva_connect.bulk_actions_run.run_assign",
	"Clear Assignment": "tatva_connect.bulk_actions_run.run_clear_assignment",
	"Bulk Edit": "tatva_connect.bulk_actions_run.run_bulk_edit",
	"Bulk Delete": "tatva_connect.bulk_actions_run.run_bulk_delete",
}


@frappe.whitelist()
def run_or_queue(action, doctype, docnames, params=None):
	"""The ONE door the frontend calls for all four actions. Runs inline under threshold or with the
	feature off; otherwise records a `CRM List Action Job` and returns its name for the tab to watch."""
	if action not in _EXECUTORS:
		frappe.throw(_("Unknown bulk action {0}.").format(action))
	docnames = frappe.parse_json(docnames) if isinstance(docnames, str) else list(docnames)
	params = frappe.parse_json(params) if isinstance(params, str) else (params or {})

	if len(docnames) > MAX_ROWS:
		frappe.throw(_("Bulk operations only support up to {0} documents.").format(MAX_ROWS))

	# At the DOOR, because neither lane can carry a refusal any later: `_bulk_action` swallows a per-row throw, and a queued batch would reach the rep as a failed count with no reason.
	if action == "Bulk Edit":
		tasks.refuse_disabled_bulk_complete(doctype, docnames, "update", {params["field"]: params["value"]})

	if not automation.is_enabled(AUTOMATION_KEY) or len(docnames) < THRESHOLD:
		result = _run(action, doctype, docnames, params)
		return {"queued": False, **result}

	job = frappe.get_doc({
		"doctype": DOCTYPE,
		"action": action,
		"target_doctype": doctype,
		"docnames": frappe.as_json(docnames),
		"params": frappe.as_json(params),
		"total": len(docnames),
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — the seam's own row, gated by the caller; owner is the requester
	return {"queued": True, "job": job.name, "status": job.status}


def run(job):
	"""The worker. Runs as the person who triggered the action (see the module docstring), so every
	permission check inside the executor is theirs, unchanged from the inline path."""
	doc = frappe.get_doc(DOCTYPE, job)
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")  # same lever as partner_bulk_worker.process_job — disarms the gap-locks that deadlock against concurrent writers; connection is fresh per RQ job so this can't leak to another job on the `long` queue
	doc.db_set({"status": "Started", "job_id": _job_id()}, update_modified=False)
	frappe.db.commit()  # the tab is watching this row's status; a drain that runs for a while must not hide it
	try:
		result = _run(doc.action, doc.target_doctype, frappe.parse_json(doc.docnames), frappe.parse_json(doc.params))
		doc.reload()
		doc.db_set({
			"status": "Completed",
			"succeeded": result["succeeded"],
			"failed": result["failed"],
			"failed_names": frappe.as_json(result["failed_names"]),
		}, update_modified=False)
		frappe.db.commit()
		_publish(EVENT_READY, doc, _result(doc))
	except Exception:
		frappe.db.rollback()
		doc.reload()
		doc.db_set({"status": "Error", "error_message": frappe.get_traceback(with_context=True)}, update_modified=False)
		frappe.db.commit()
		frappe.log_error(f"bulk action failed: {doc.action} / {doc.target_doctype}", doc.error_message)
		_publish(EVENT_FAILED, doc, _result(doc))


def _run(action, doctype, docnames, params):
	executor = frappe.get_attr(_EXECUTORS[action])
	return executor(doctype, docnames, params)


def _publish(event, doc, payload):
	frappe.publish_realtime(event, {"job": doc.name, "action": doc.action, **payload}, user=doc.owner)


def _job_id():
	from rq import get_current_job

	job = get_current_job()
	return job.id if job else None


@frappe.whitelist()
def status(job):
	"""Where one bulk job has got to. Reading the job row IS the gate: the doctype is `if_owner`, so
	another rep's job is not readable, matching `tatva_connect.exports.status`."""
	frappe.has_permission(DOCTYPE, "read", doc=job, throw=True)
	return _result(frappe.get_doc(DOCTYPE, job))


@frappe.whitelist()
def mine(minutes=15):
	"""The caller's own recent bulk jobs — what a tab needs to catch up after it stopped listening."""
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
	out = {"job": doc.name, "action": doc.action, "status": doc.status,
	       "total": doc.total, "succeeded": doc.succeeded, "failed": doc.failed,
	       "creation": doc.creation}
	if doc.status == "Completed":
		out["failed_names"] = frappe.parse_json(doc.failed_names) if doc.failed_names else []
	elif doc.status == "Error":
		out["error"] = _("The bulk action could not complete.")  # the traceback stays with the operator
	return out
