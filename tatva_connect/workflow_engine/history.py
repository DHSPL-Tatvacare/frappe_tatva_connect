# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Run history — the read surface over what the engine already wrote.

WHY THIS EXISTS
---------------
When a lead does not get her task or her message, nobody can answer why. The engine already
records everything the answer needs: `CRM Workflow Run` says where a run sits and what it is
waiting for, and `CRM Workflow Step Log` says which node ran, what it decided, what it said and
how long it took. Neither had a surface, so the answer existed and was unreachable.

Four questions, four endpoints, one each — never one endpoint with a mode flag:
  `runs_for_subject`  which runs exist for this record, and where each one sits now
  `run_steps`         what happened, in order
  `run_state`         why it stopped — parked on what, failed for what reason, or done
  `stuck_runs`        which runs nobody will come back to, so an operator sees them unprompted

IT STORES NOTHING
-----------------
Every value here is read off the run row or DERIVED from the step log at answer time. There is no
summary column, no step counter and no second status, because any of those could drift from the
log it claims to summarise. A failed run's reason is the detail of its last `failed` step — the
row `interpreter._fail` wrote — not a copy of it. "Stuck" is a predicate over the run's own park
columns, not a fifth value of `status`.

The one derived value that costs a query is the failure reason. It was deliberately kept out of
`runs_for_subject` to avoid an N+1 on the page that loads first — and that was reversed on
2026-07-22, because the list could then say "Failed at send_welcome" and never say because what,
which is the one thing the reader opened the tab for. The cost is bounded by construction:
`_failure` returns None on every status but `Failed` before it touches the log, so a page of runs
asks once per FAILED run, not once per run.

THE GATE
--------
Run history is patient data — `state_json` holds the lead's field values and a step `detail` can
quote them. So every endpoint answers through one question, asked of the ONE brain:
`access.visibility.parent_readable(subject_doctype, subject_name)`. That is the same rule
`scoped_has_permission` applies to every other child of a lead, called directly because these
endpoints already know the parent. Missing and out-of-scope answer identically, so the surface
cannot be used to probe for record ids.

The rule is asked here rather than left to the `CRM Workflow Run` DocPerm on purpose. The doctype
grants read to System Manager / Automation Manager / Sales Manager and to no rep, and widening it
to every Sales User would open the whole table to a report view. The endpoint is the boundary; the
lead is the thing being authorised.
"""
import frappe
from frappe import _

from tatva_connect.access import visibility
from tatva_connect.workflow_engine import interpreter

RUN_DT = "CRM Workflow Run"
STEP_LOG_DT = "CRM Workflow Step Log"
EVENT_DT = "CRM Workflow Event"

MAX_RUNS = 100
MAX_STEPS = 500

_RUN_FIELDS = [
	"name", "workflow", "workflow_version", "status", "current_node",
	"resume_at", "awaiting_signal", "awaiting_correlation", "retry_count",
	"subject_doctype", "subject_name", "trigger_doctype", "trigger_name",
	"creation", "modified",
]
_STEP_FIELDS = ["name", "node_id", "node_type", "outcome", "detail", "duration_ms", "creation"]


@frappe.whitelist()
def runs_for_subject(subject_doctype, subject_name, limit=20, start=0):
	"""Which runs exist for one record, newest first, and where each one currently sits."""
	_assert_readable(subject_doctype, subject_name)
	size, offset = _bounded(limit, MAX_RUNS), _offset(start)
	# `get_all` (not `get_list`): the caller was just authorised for this exact subject and the filter
	# pins every row to it, so the row set cannot exceed the gate. `get_list` would additionally demand
	# a `CRM Workflow Run` DocPerm the rep whose lead this is deliberately does not have.
	rows = frappe.get_all(  # authz-ok: tier-b — gated by _assert_readable() on this exact subject, above
		RUN_DT,
		filters={"subject_doctype": subject_doctype, "subject_name": subject_name},
		fields=_RUN_FIELDS,
		order_by="creation desc, name desc",
		limit=size + 1,
		offset=offset,
	)
	return {"runs": [_summary(row) for row in rows[:size]], "has_more": len(rows) > size}


@frappe.whitelist()
def run_steps(run, limit=200, start=0):
	"""What happened, in order: every node this run executed, its outcome, its detail, its duration."""
	_readable_run(run)
	size, offset = _bounded(limit, MAX_STEPS), _offset(start)
	# `creation` then `name`: the doctype sorts by creation, but a whole segment is written inside one
	# transaction and can share a timestamp, so the autoincrement name is what makes the order the
	# EXECUTION order rather than an arbitrary one.
	rows = frappe.get_all(  # authz-ok: tier-b — gated by _readable_run() on this exact run, above
		STEP_LOG_DT,
		filters={"workflow_run": run},
		fields=_STEP_FIELDS,
		order_by="creation asc, name asc",
		limit=size + 1,
		offset=offset,
	)
	return {"steps": rows[:size], "has_more": len(rows) > size}


@frappe.whitelist()
def run_state(run):
	"""Why this run stopped — parked on what and until when, failed for what reason, or done."""
	row = _readable_run(run)
	return dict(
		_summary(row),
		failure=_failure(row),
		steps=frappe.db.count(STEP_LOG_DT, {"workflow_run": row.name}),
	)


@frappe.whitelist()
def stuck_runs(limit=25, start=0, workflow=None):
	"""Runs nobody will come back to, newest activity first, so an operator sees them unprompted.

	Two disjoint sets by construction — a run is Failed or it is Parked, never both — so they are two
	filters rather than one predicate that would have to re-derive the disjunction in SQL.
	"""
	if not frappe.has_permission(RUN_DT, "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	size, offset = _bounded(limit, MAX_RUNS), _offset(start)
	base = {"workflow": workflow} if workflow else {}
	window = size + offset + 1
	rows = _stuck_page({**base, "status": "Failed"}, window) + _stuck_page(
		{**base, "status": "Parked", "resume_at": ["is", "not set"], "awaiting_signal": ["is", "not set"]},
		window,
	)
	rows.sort(key=lambda row: row.modified, reverse=True)
	visible = [row for row in rows if visibility.parent_readable(row.subject_doctype, row.subject_name)]
	return {
		"runs": [_summary(row) for row in visible[offset : offset + size]],
		"has_more": len(visible) > offset + size,
	}


@frappe.whitelist()
def node_counts(workflow, workflow_version=None):
	"""How many runs are RESTING on each node — `{"waiting": {node: n}, "failed": {node: n}}`.

	A run only comes to rest in two situations, because the interpreter walks a whole segment in one pass:
	it PARKS (only a Wait parks a run) or it DIES. Everywhere else it is present for milliseconds, so a
	count on a Route would read 0 for ever and be noise dressed as information. Running and Done are
	counted nowhere: one is passing through, the other is not anywhere.

	The two numbers are never added. They mean different things and they are read in different colours —
	a node carrying both must report both, or an author reads a real fault as a queue.

	Version-scoped: `b1` in v3 and `b1` in v4 may be different nodes, so a figure summed across versions
	describes no graph that ever existed. `workflow_version` is optional only so a caller may ask about the
	whole workflow deliberately; the canvas always passes the version it is showing.

	Scoped by `get_list`, exactly as `_stuck_page` is, so the run table's registered
	permission_query_conditions apply IN SQL rather than through a second rule written here.

	Counted by the database, through `get_list`'s own function syntax — `{"COUNT": "*", "as": "total"}`
	with a GROUP BY. Frappe refuses a raw `count(name) as total` string outright, and it is right to: the
	dict form is the platform's answer and it carries its own alias, so nothing here builds SQL or reads
	back N rows to length them.

	The signature is free to take a tick later (Phase P): a scheduled workflow will want these split by the
	tick that produced them, and that must be an added argument, not a rewrite.
	"""
	if not frappe.has_permission("CRM Workflow", "read", workflow):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	filters = {"workflow": workflow, "status": ("in", ("Parked", "Failed"))}
	if workflow_version:
		filters["workflow_version"] = workflow_version

	found = {"waiting": {}, "failed": {}}
	for row in frappe.get_list(
		RUN_DT,
		filters=filters,
		fields=["status", "current_node", {"COUNT": "*", "as": "total"}],
		group_by="status, current_node",
	):
		if not row.current_node:
			continue  # a run that died before it reached a node has nowhere to be counted
		bucket = found["waiting"] if row.status == "Parked" else found["failed"]
		bucket[row.current_node] = row.total
	return found


def _stuck_page(filters, window):
	"""One bounded page off the run table through `get_list`, so the row-visibility brain's
	permission_query_conditions apply in SQL when the operator has switched them on. The
	`parent_readable` pass in the caller is the same rule's single-row twin, and is what keeps the
	answer correct while that switch is still dormant."""
	return frappe.get_list(
		RUN_DT,
		filters=filters,
		fields=_RUN_FIELDS,
		order_by="modified desc",
		limit=window,
		offset=0,
	)


def _summary(row):
	"""One run as the UI needs it: its own columns, plus the two things that are only ever derived."""
	return {
		"run": row.name,
		"workflow": row.workflow,
		"workflow_version": row.workflow_version,
		"status": row.status,
		"current_node": row.current_node,
		"subject_doctype": row.subject_doctype,
		"subject_name": row.subject_name,
		"trigger_doctype": row.trigger_doctype,
		"trigger_name": row.trigger_name,
		"retry_count": row.retry_count or 0,
		"started": row.creation,
		"last_activity": row.modified,
		"waiting_on": _waiting_on(row),
		"stuck": _is_stuck(row),
		# The third derived answer; costs a query only for a run that really failed.
		"failure": _failure(row),
	}


def _waiting_on(row):
	"""What a parked run is waiting for — the three park columns `interpreter._park` wrote and the
	sweep wakes it by, plus whether a signal it named is actually buffered.

	`None` for a run that is not parked: a finished run waits on nothing, and reporting its last
	deadline would read as if it still might move.
	"""
	if row.status != "Parked":
		return None
	signal = row.awaiting_signal or None
	return {
		"resume_at": row.resume_at,
		"signal": signal,
		"correlation": row.awaiting_correlation or None,
		"signal_pending": _signal_pending(row) if signal else False,
	}


def _signal_pending(row):
	"""Is an inbox row buffered that this park would consume? Asked through the interpreter's own
	filter builder, so "would wake it" means here exactly what it means at the consume side."""
	filters = interpreter.pending_signal_filters(
		row.subject_doctype, row.subject_name, row.awaiting_signal, row.awaiting_correlation
	)
	return bool(frappe.db.exists(EVENT_DT, filters))


def _failure(row):
	"""Why a failed run failed, DERIVED: the last `failed` step of its log, which is the row
	`interpreter._fail` wrote. Nothing is copied onto the run, so this can never disagree with the log
	it is read from. `None` for a run that has not failed."""
	if row.status != "Failed":
		return None
	rows = frappe.get_all(
		STEP_LOG_DT,
		filters={"workflow_run": row.name, "outcome": "failed"},
		fields=["node_id", "detail", "creation"],
		order_by="name desc",
		limit=1,
	)
	return rows[0] if rows else None


def _is_stuck(row):
	"""A run nobody will come back to: Failed (terminal by construction — `_fail` drops `active_key`
	and nothing re-drives it), or Parked with neither a clock to wake it nor a signal named to wake it
	by.

	A park that NAMES a signal is not called stuck even when no inbox row is buffered: the signal can
	still arrive, and only a TTL could say it never will. `waiting_on.signal_pending` reports that case
	as what it is instead of guessing — settling those is the waiting-tail sweep's job, not a read
	surface's.
	"""
	if row.status == "Failed":
		return True
	return row.status == "Parked" and not row.resume_at and not row.awaiting_signal


def _assert_readable(subject_doctype, subject_name):
	"""The gate. Delegates to the ONE row-visibility brain; missing and out-of-scope throw the same
	error, so the surface cannot be used to probe for record ids."""
	if not visibility.parent_readable(subject_doctype, subject_name):
		frappe.throw(_("Not found"), frappe.DoesNotExistError)


def _readable_run(run):
	"""One run's columns, after gating on the record it is about. The row is read permission-free on
	purpose — it is read in order to find out WHICH subject to authorise, and the next line is the
	authorisation. A run that does not exist and a run on another rep's line throw identically."""
	row = frappe.db.get_value(RUN_DT, run, _RUN_FIELDS, as_dict=True) if run else None
	if not row:
		frappe.throw(_("Not found"), frappe.DoesNotExistError)
	_assert_readable(row.subject_doctype, row.subject_name)
	return row


def _bounded(value, ceiling):
	"""A caller's page size, clamped. Every query here takes a limit and no caller can lift it: a lead
	with 500 runs must not be able to ask for all of them, nor for all of their steps."""
	try:
		size = int(value)
	except (TypeError, ValueError):
		size = 0
	return max(1, min(size or ceiling, ceiling))


def _offset(value):
	try:
		return max(0, int(value))
	except (TypeError, ValueError):
		return 0
