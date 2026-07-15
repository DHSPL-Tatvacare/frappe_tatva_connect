"""The entry trigger - the ONE seam that starts an Instance, on the wildcard `doc_events["*"]` (the
automation router's proven precedent), guarded by its OWN `frappe.flags.in_workflow` re-entrancy flag so
it coexists with the automation engine's `in_automation` guard and neither engine fires the other.

On the entry doctype's `entry_event` (Created/Updated/Deleted), every ENABLED Definition whose grain
matches the subject starts: an Instance is created AND its first segment runs in ONE transaction,
committing at the first suspend (F3 - no `Running` orphan if it crashes before the first park). The
`active_key` UNIQUE index rejects a duplicate start; that `IntegrityError` is caught and treated as
"already running", never surfaced (F3 double-start guard, closed at the DB).

Dormant-by-default (constitution A.6): with the engine switch off, nothing starts. The wildcard fires on
EVERY write of EVERY doctype, so the switch check + a cheap enabled-Definition lookup early-return before
any real work.
"""
import frappe

from tatva_connect import automation
from tatva_connect.workflow_engine import ENGINE_SWITCH, interpreter, versions

INSTANCE_DT = interpreter.INSTANCE_DT
_DEF_DT = "CRM Workflow Definition"


def on_created(doc, method=None):
	_maybe_start(doc, "Created")


def on_updated(doc, method=None):
	_maybe_start(doc, "Updated")


def on_trash(doc, method=None):
	_maybe_start(doc, "Deleted")


# A Frappe lifecycle event AS a signal source (design §12): a rep marking a workflow-raised CRM Task
# Done delivers `review_done` to the journey parked on it - NO API call, a real doc_event. Correlation
# linkage (the one genuinely new bit): the Task carries no workflow token, so the detector matches on the
# Task's own lead (reference_docname) + the ONE Instance parked on `review_done` for that lead, and
# delivers with THAT Instance's awaiting_correlation - so the wake matches the exact iteration's wait and
# a stale/duplicate delivery cannot cross iterations (F2). Idempotent by construction: once the journey
# advances past the review Wait it is no longer Parked on `review_done`, so a second Task-Done save finds
# no parked Instance and delivers nothing - marking Done twice never double-advances.
_REVIEW_SIGNAL = "review_done"
_DONE_STATUSES = frozenset({"Done", "Completed", "Closed"})


def on_task_done(doc, method=None):
	"""Wildcard `doc_events["*"]["on_update"]` detector: a CRM Task flipping to Done delivers `review_done`
	to the Instance parked on it. Dormant-by-default (engine switch off → nothing); guarded by `in_workflow`
	so a Task the engine itself created/completed cannot re-enter; cheap early-returns for every non-Task,
	non-Done, non-Lead-linked write (the wildcard fires on EVERY doctype's update)."""
	if frappe.flags.get("in_workflow"):
		return  # re-entrancy guard: a Task write the engine made must not re-enter signal detection
	if doc.doctype != "CRM Task" or (doc.get("status") or "") not in _DONE_STATUSES:
		return
	if doc.get("reference_doctype") != "CRM Lead" or not doc.get("reference_docname"):
		return
	if not automation.is_enabled(ENGINE_SWITCH):
		return
	lead = doc.reference_docname
	parked = frappe.db.get_value(
		INSTANCE_DT,
		{"subject_doctype": "CRM Lead", "subject_name": lead, "awaiting_signal": _REVIEW_SIGNAL, "status": "Parked"},
		["name", "awaiting_correlation"],
		as_dict=True,
	)
	if not parked:
		return  # no journey is waiting on this task's review — nothing to signal (idempotent re-fire)
	from tatva_connect.workflow_engine import signals

	signals.deliver_signal(
		"CRM Lead", lead, _REVIEW_SIGNAL, correlation=parked.awaiting_correlation, payload={"verdict": doc.status}
	)


def _maybe_start(doc, event):
	"""Start every enabled, grain-matching Definition whose (entry_doctype, entry_event) match this write."""
	if frappe.flags.get("in_workflow"):
		return  # re-entrancy guard: a write the engine made must not re-enter entry detection
	if not automation.is_enabled(ENGINE_SWITCH):
		return
	definitions = frappe.get_all(
		_DEF_DT,
		filters={"enabled": 1, "entry_doctype": doc.doctype, "entry_event": event},
		fields=["name", "vertical", "group", "program"],
	)
	if not definitions:
		return
	axes = _subject_axes(doc)
	for d in definitions:
		if _grain_matches(d, axes):
			_start_one(d.name, doc)


def _subject_axes(doc):
	"""(vertical, group, program) of the subject. A CRM Lead resolves through the ONE accessor the
	automation/activity engines use; a non-Lead subject has no grain axes."""
	if doc.doctype == "CRM Lead":
		from tatva_connect.automation import rules

		return rules.lead_axes(doc.name)
	return (None, None, None)


def _grain_matches(definition, axes):
	"""A blank Definition axis is a wildcard (mirrors `rules.matching_rules`); a set axis must equal the
	subject's. A non-Lead subject (axes all None) matches only a fully-wildcard Definition."""
	for want, got in zip((definition.vertical, definition.group, definition.program), axes):
		if want and want != (got or ""):
			return False
	return True


def _start_one(workflow_name, doc):
	"""Create the Instance at the graph's entry node (the first node) and run its first segment in ONE
	transaction, committing at the first suspend (advance). The `active_key` UNIQUE index closes the
	double-start race - a second entry for the same (workflow, subject) raises IntegrityError on insert,
	which is caught and treated as already-running."""
	version_name = versions.current_name(workflow_name)
	entry_node = versions.load(version_name).nodes[0].node_id
	frappe.flags.in_workflow = True  # the first segment's own writes must not re-enter entry detection
	try:
		instance = frappe.get_doc({
			"doctype": INSTANCE_DT,
			"workflow": workflow_name,
			"workflow_version": version_name,
			"subject_doctype": doc.doctype,
			"subject_name": doc.name,
			"current_node": entry_node,
			"state_json": "{}",
			"status": "Running",
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — workflow engine, entry trigger
		interpreter.advance(instance)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback()  # active_key UNIQUE rejected a second live Instance - already running (F3)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="workflow: entry start failed", message=f"workflow={workflow_name} subject={doc.name} :: {frappe.get_traceback()}")
	finally:
		frappe.flags.in_workflow = False
