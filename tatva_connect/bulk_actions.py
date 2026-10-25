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

WHY A DURABLE ROW, NOT JUST A SOCKET EVENT. `frappe.publish_progress` is fire-and-forget: a tab not
connected at that instant loses it. So the job row is the truth, written as the run goes, and every write
publishes it on the one path `tatva_connect.job_feed` owns, the same one exports use.

THE EXECUTOR REGISTRY IS CLOSED. `action` is a Select and this map is the only way it becomes code.

PERMISSIONS ARE THE CALLER'S. `frappe.enqueue` captures `{"user": frappe.session.user}` and
`execute_job` calls `frappe.set_user(user)`, so a worker runs as the person who clicked the action and
every one of frappe's own permission checks inside `add`/`remove`/`_bulk_action`/`delete_bulk` applies
unchanged. The two `ignore_permissions=True` calls below are on the JOB ROW ITSELF, this seam's own
scaffolding — never on the mutation.
"""
import frappe
from frappe import _

from tatva_connect import bulk_actions_run, job_feed
from tatva_connect.notifications import batch
from tatva_connect.tasks import tasks
from tatva_connect.utils import read_committed

DOCTYPE = "CRM List Action Job"

# Frappe's own bulk_update.py refuses above this; restoring the same ceiling here since this seam
# calls the executor directly and no longer goes through that dispatcher's own check.
MAX_ROWS = 500

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


@frappe.whitelist(methods=["POST"])
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
	job_feed.publish("bulk", job, after_commit=True)  # the insert commits when this request ends
	return {"queued": True, "job": job.name, "status": job.status}


def run(job):
	"""The worker. Runs as the person who triggered the action (see the module docstring), so every
	permission check inside the executor is theirs, exactly as if they had run it in their own request."""
	read_committed()  # connection is fresh per RQ job, so this cannot leak to another job on the `long` queue
	if not (doc := job_feed.start("bulk", job)):
		return  # stopped before a worker reached it
	try:
		docnames = frappe.parse_json(doc.docnames)
		params = frappe.parse_json(doc.params)
		_assert_may_run(doc.action, doc.target_doctype, docnames, params)  # a job row outlives the request that made it
		result = _run(doc.action, doc.target_doctype, docnames, params, _counter(doc), doc.name)
		job_feed.finish("bulk", doc, {
			"succeeded": result["succeeded"],
			"failed": result["failed"],
			# Each failed record with the reason its run kept in hand, so the panel shows why without a query.
			"failed_names": frappe.as_json([{"name": n, "reason": result.get("reasons", {}).get(n)} for n in result["failed_names"]]),
		})
	except Exception:
		job_feed.fail("bulk", doc, f"bulk action failed: {doc.action} / {doc.target_doctype}")  # logs the traceback


def _run(action, doctype, docnames, params, step, job=None):
	executor = frappe.get_attr(_EXECUTORS[action])
	with batch.holding(f"{DOCTYPE}:{job}" if job else None, doctype):  # the run's assignments are told once per wave, by notifications/batch.py
		return executor(doctype, docnames, params, step)


def _counter(doc):
	"""The executors' `step`: running totals, kept in Redis after every record and told to the owner's tabs at the feed's pace."""
	report, done = job_feed.progress("bulk", doc), {"succeeded": 0, "failed": 0}

	def step(succeeded=0, failed=0):
		done["succeeded"] += succeeded
		done["failed"] += failed
		report(dict(done))

	return step


def _result(doc):
	status = "Error" if job_feed.abandoned(DOCTYPE, doc) else doc.status
	docnames = frappe.parse_json(doc.docnames or "[]")
	out = {"job": doc.name, "action": doc.action, "status": status, "target_doctype": doc.target_doctype,
	       "total": doc.total, "succeeded": doc.succeeded, "failed": doc.failed, "creation": doc.creation,
	       "modified": doc.modified, "params": frappe.parse_json(doc.params or "{}"),
	       # Only a live delete names its records: a record's page goes read-only while it runs, and no other row needs 500 names.
	       "docnames": docnames if doc.action == "Bulk Delete" and status in job_feed.LIVE else [],
	       "subject": docnames[0] if len(docnames) == 1 else None,
	       # The row's own state: a job past its timeout still has a live row to close, so it can be aborted too.
	       "abortable": doc.status in job_feed.LIVE,
	       "aborted": job_feed.stopped(doc)}
	if status == "Completed":
		# A job from before reasons were kept stored bare names; both read as {name, reason}.
		out["failed_names"] = [f if isinstance(f, dict) else {"name": f, "reason": None}
		                       for f in (frappe.parse_json(doc.failed_names) if doc.failed_names else [])]
	elif out["aborted"]:
		out["error"] = _("Stopped by you.")
	elif status == "Error":
		out["error"] = (_("The job did not finish.") if doc.status != "Error"
		                else _("The bulk action could not complete."))  # the traceback stays with the operator
	return out
