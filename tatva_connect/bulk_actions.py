# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE list-action seam: Assign / Clear Assignment / Reassign / Bulk Edit / Bulk Delete ALWAYS run on a
worker — one row or five hundred, from a list selection or from a record's own header.

NO THRESHOLD. A row count cannot predict a delete's cost — one lead of a busy programme outweighs fifty
quiet ones — and the inline path it chose held locks across `tabCRM Task`/`tabCRM Call Log`/`tabToDo`
inside the rep's own request, deadlocking against the workers writing the same rows.

Frappe's own dispatchers each pick their own threshold — `bulk_update.submit_cancel_or_update_docs` past
20, `reportview.delete_items` past 10, `assign_to.add_multiple`/`remove_multiple` never. This seam calls
the INNERMOST function each one calls, so none of those dispatchers is reached and none of their
thresholds can fire. `Bulk Delete` also reuses CRM's own `get_linked_docs_of_document`/
`remove_linked_doc_reference` to cascade linked documents, as the CRM app's own Delete button does.

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

from tatva_connect import bulk_actions_run
from tatva_connect.tasks import tasks

DOCTYPE = "CRM List Action Job"

# Frappe's own bulk_update.py refuses above this; restoring the same ceiling here since this seam
# calls the executor directly and no longer goes through that dispatcher's own check.
MAX_ROWS = 500

# Realtime events, spelled once so the worker and the SPA cannot drift; all four are `user=`-targeted.
# A job announces itself at BIRTH and again at each state change, the way `crm_notification` does — the
# panel used to hear only READY/FAILED, so a queued job was invisible for the whole window a person is
# actually watching it and only appeared after a page refresh re-ran `mine`.
EVENT_QUEUED = "crm_bulk_queued"
EVENT_STARTED = "crm_bulk_started"
EVENT_READY = "crm_bulk_ready"
EVENT_FAILED = "crm_bulk_failed"

# How many recent jobs `mine` answers with. A tab is catching up on its own jobs, not browsing history.
_RECENT = 10

# action -> executor; each returns {"total", "succeeded", "failed", "failed_names"}. Closed by construction.
_EXECUTORS = {
	"Assign": "tatva_connect.bulk_actions_run.run_assign",
	"Clear Assignment": "tatva_connect.bulk_actions_run.run_clear_assignment",
	# Clear then assign, per row — the two above in one pass, never a third way to assign.
	"Reassign": "tatva_connect.bulk_actions_run.run_reassign",
	"Bulk Edit": "tatva_connect.bulk_actions_run.run_bulk_edit",
	"Bulk Delete": "tatva_connect.bulk_actions_run.run_bulk_delete",
}


def _assert_may_run(action, doctype, docnames, params):
	"""The door's rules, asked at BOTH doors. `run_or_queue` is one; the worker is the other, because a
	`CRM List Action Job` row is directly insertable by any role that may create one, and `after_insert`
	enqueues the drain — so a caller who writes the row instead of calling the endpoint would otherwise
	skip every rule below. `produce_export` re-asks its gate in the worker for the same reason."""
	if action not in _EXECUTORS:
		frappe.throw(_("Unknown bulk action {0}.").format(action))
	if len(docnames) > MAX_ROWS:
		frappe.throw(_("Bulk operations only support up to {0} documents.").format(MAX_ROWS))
	# Neither lane can carry a refusal any later: `_bulk_action` swallows a per-row throw, and a queued batch would reach the rep as a failed count with no reason.
	if action == "Bulk Edit":
		tasks.refuse_disabled_bulk_complete(doctype, docnames, "update", bulk_actions_run.edit_values(params))


@frappe.whitelist()
def run_or_queue(action, doctype, docnames, params=None):
	"""The ONE door for every list action, from a selection or from a record's own header — always queued."""
	docnames = frappe.parse_json(docnames) if isinstance(docnames, str) and docnames.strip() else list(docnames)
	params = frappe.parse_json(params) if isinstance(params, str) and params.strip() else (params or {})
	_assert_may_run(action, doctype, docnames, params)

	job = frappe.get_doc({
		"doctype": DOCTYPE,
		"action": action,
		"target_doctype": doctype,
		"docnames": frappe.as_json(docnames),
		"params": frappe.as_json(params),
		"total": len(docnames),
	}).insert(ignore_permissions=True)  # authz-ok: tier-a — the seam's own row, gated by the caller; owner is the requester
	_publish(EVENT_QUEUED, job, _result(job), after_commit=True)
	return {"queued": True, "job": job.name, "status": job.status}


def run(job):
	"""The worker. Runs as the person who triggered the action (see the module docstring), so every
	permission check inside the executor is theirs, exactly as if they had run it in their own request."""
	doc = frappe.get_doc(DOCTYPE, job)
	frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")  # same lever as partner_bulk_worker.process_job — disarms the gap-locks that deadlock against concurrent writers; connection is fresh per RQ job so this can't leak to another job on the `long` queue
	doc.db_set({"status": "Started", "job_id": _job_id()}, update_modified=False)
	frappe.db.commit()  # the tab is watching this row's status; a drain that runs for a while must not hide it
	_publish(EVENT_STARTED, doc, _result(doc))
	try:
		docnames = frappe.parse_json(doc.docnames)
		params = frappe.parse_json(doc.params)
		_assert_may_run(doc.action, doc.target_doctype, docnames, params)  # a job row outlives the request that made it
		result = _run(doc.action, doc.target_doctype, docnames, params)
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


def _publish(event, doc, payload, after_commit=False):
	"""Every event carries `running`, the way `crm_notification` carries `unread`: the panel's badge moves
	on the payload alone and only refetches the list when it is open.

	`after_commit` for the QUEUED one: `run_or_queue` is a whitelisted method, so its insert is not
	committed until the request ends. Announced any earlier, the panel reloads `mine` on a connection
	that cannot see the row yet and draws exactly the blank list this event exists to prevent. The
	worker's own events already publish after its explicit commits."""
	frappe.publish_realtime(
		event,
		{"job": doc.name, "action": doc.action, "running": _running(doc.owner), **payload},
		user=doc.owner,
		after_commit=after_commit,
	)


def _running(user):
	return frappe.db.count(DOCTYPE, {"owner": user, "status": ("in", ("Queued", "Started"))})


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
	recent = frappe.get_list(
		DOCTYPE,
		filters={"owner": frappe.session.user, "creation": [">", since]},
		pluck="name",
		order_by="creation desc",
		limit=_RECENT,
	)
	# A job still running outlives the window it started in, and a record greyed out by it depends on this answer.
	running = frappe.get_list(
		DOCTYPE,
		filters={"owner": frappe.session.user, "status": ("in", ("Queued", "Started"))},
		pluck="name",
		order_by="creation desc",
		limit=_RECENT,
	)
	names = list(dict.fromkeys(running + recent))
	return [_result(frappe.get_doc(DOCTYPE, name)) for name in names]


def _result(doc):
	# `target_doctype` + `docnames`: a record's page asks whether THIS row is going, and the job row holds both.
	out = {"job": doc.name, "action": doc.action, "status": doc.status,
	       "target_doctype": doc.target_doctype, "docnames": frappe.parse_json(doc.docnames or "[]"),
	       "total": doc.total, "succeeded": doc.succeeded, "failed": doc.failed,
	       "creation": doc.creation}
	if doc.status == "Completed":
		out["failed_names"] = frappe.parse_json(doc.failed_names) if doc.failed_names else []
	elif doc.status == "Error":
		out["error"] = _("The bulk action could not complete.")  # the traceback stays with the operator
	return out
