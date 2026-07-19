"""The entry trigger - the ONE seam that starts an Instance, on the wildcard `doc_events["*"]` (the
automation router's proven precedent), guarded by its OWN `frappe.flags.in_workflow` re-entrancy flag so
it coexists with the automation engine's `in_automation` guard and neither engine fires the other.

On the entry doctype's `entry_event` (Created/Updated/Deleted), every ACTIVE Definition whose grain
matches the subject starts: an Instance is created AND its first segment runs in ONE transaction,
committing at the first suspend (F3 - no `Running` orphan if it crashes before the first park). The
`active_key` UNIQUE index rejects a duplicate start; that `IntegrityError` is caught and treated as
"already running", never surfaced (F3 double-start guard, closed at the DB).

Dormant-by-default (constitution A.6): with the engine switch off, nothing starts. The wildcard fires on
EVERY write of EVERY doctype, so the switch check + a cheap Active-Definition lookup early-return before
any real work.
"""
import frappe

from tatva_connect import automation
from tatva_connect.automation import rules
from tatva_connect.tatva_connect.doctype.crm_workflow_definition.crm_workflow_definition import ARMED_STATE
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


def run_guards(doc, method=None):
	"""Wildcard `validate` — the SYNCHRONOUS GUARD lane for Flows (D3). Before the save commits, every
	ACTIVE Flow matching this record's (doctype, event) + grain + When runs its guard-lane action items;
	a handler raising propagates straight out of validate and BLOCKS the save (never swallowed). Guards
	are Flows too: a Require Location / Require Fields Flow enforces at save time, every other action runs
	after — there is no separate guard engine.

	Reuses the automation engine's ONE context builder + criteria evaluator + guard handlers — no second
	copy (the fold shares one vocabulary). Dormant-by-default (engine switch) and non-re-entrant
	(`in_workflow`), so a write the engine itself made never re-enters its own guard lane."""
	if frappe.flags.get("in_workflow"):
		return
	if not automation.is_enabled(ENGINE_SWITCH):
		return
	ctx = _trigger_context(doc, "Created" if doc.is_new() else "Updated")
	if ctx is None:
		return
	from tatva_connect.automation import actions

	for version_name in ctx.versions:
		version = versions.load(version_name)
		if not rules.criteria_match(version.criteria, ctx.context, ctx.field_types):
			continue  # the When did not hold — this Flow does not act on this save
		for node in version.nodes:
			if node.get("node_type") != "Step":
				continue
			for raw in (node.get("_frozen_items") or []):
				item = frappe._dict(raw)
				lane, handler = actions._ACTION_LANES.get(item.action_type, (None, None))
				if lane == "guard":
					handler(item, ctx.subject, ctx.context)  # a raise here IS the block (reaches validate unswallowed)


def covering_location_guard(doc):
	"""True iff an ACTIVE Flow with a Require Location guard already covers THIS save — its guard lane
	ran (or will run) synchronously in the same validate. The location backstop (tasks.enforce_location)
	reads this to STAND DOWN instead of double-guarding: the Flow-era replacement for the old
	"does a Require Location rule cover this?" check the rule engine used. Non-re-entrant + dormant like
	the guard lane itself."""
	if frappe.flags.get("in_workflow"):
		return False
	if not automation.is_enabled(ENGINE_SWITCH):
		return False
	ctx = _trigger_context(doc, "Created" if doc.is_new() else "Updated")
	if ctx is None:
		return False
	for version_name in ctx.versions:
		version = versions.load(version_name)
		if not rules.criteria_match(version.criteria, ctx.context, ctx.field_types):
			continue
		for node in version.nodes:
			if node.get("node_type") != "Step":
				continue
			for raw in (node.get("_frozen_items") or []):
				if frappe._dict(raw).action_type == "Require Location":
					return True
	return False


def _maybe_start(doc, event):
	"""The after-save lane: run every ACTIVE Flow whose (entry_doctype, entry_event) + grain + When match
	this write. A wait-free Flow runs inline and persists nothing (EPHEMERAL, D4); a Flow that parks starts
	a durable Instance (CONTINUOUS). Guard-lane actions already ran (or blocked the save) in `run_guards`."""
	if frappe.flags.get("in_workflow"):
		return  # re-entrancy guard: a write the engine made must not re-enter entry detection
	if not automation.is_enabled(ENGINE_SWITCH):
		return
	ctx = _trigger_context(doc, event)
	if ctx is None:
		return
	for version_name in ctx.versions:
		version = versions.load(version_name)
		if not rules.criteria_match(version.criteria, ctx.context, ctx.field_types):
			continue  # the When did not hold — this Flow does not act on this write
		if interpreter.has_wait(version):
			_start_one(version.workflow, version_name, ctx.subject, ctx.context)  # CONTINUOUS: durable Instance on the lead
		else:
			_run_ephemeral(version_name, ctx.subject, doc, ctx.context)  # EPHEMERAL: run inline, persist nothing


def _trigger_context(doc, event):
	"""Shared setup for both Flow lanes (guard + effect), reusing the automation engine's ONE brains. The
	cheap Definition query gates everything, so an unrelated save resolves no subject and builds no context.
	Returns `_dict(subject, context, field_types, versions)` — the resolved parent LEAD (the effect verbs'
	subject, D7), the trigger context the When reads (with `{field}__before` for an Updated diff), the
	field-type map for type-aware criteria, and the current frozen version of each grain-matched Flow — or
	`None` when nothing can match (fail-closed)."""
	definitions = frappe.get_all(
		_DEF_DT,
		filters={"lifecycle_state": ARMED_STATE, "entry_doctype": doc.doctype, "entry_event": event},
		fields=["name", "vertical", "group", "program"],
	)
	if not definitions:
		return None
	from tatva_connect.automation import context as ctx_build

	subject = ctx_build.subject(doc)
	if subject is None:
		return None  # no resolvable parent lead → no Flow can act (fail-closed)
	axes = ctx_build.subject_axes(subject)
	matched = [d for d in definitions if _grain_matches(d, axes)]
	if not matched:
		return None
	changed = ctx_build.diff_watched_fields(doc) if event == "Updated" else {}
	return frappe._dict(
		subject=subject.name,
		context=ctx_build.context_for(doc, changed),
		field_types=ctx_build.field_types_for(doc.doctype),
		versions=[versions.current_name(d.name) for d in matched],
	)


def _run_ephemeral(version_name, lead_name, trigger_doc, context):
	"""Run a wait-free Flow inline (D4). An ephemeral effect can DO but never DENY: `run_inline`'s savepoint
	isolates its writes and any failure is logged, never propagated, so the triggering save is untouched.
	`in_workflow` guards the effects' own writes from re-entering the front-door."""
	frappe.flags.in_workflow = True
	try:
		interpreter.run_inline(version_name, lead_name, trigger_doc, context)
	except Exception:
		frappe.log_error(title="workflow: ephemeral run failed", message=f"version={version_name} subject={lead_name} :: {frappe.get_traceback()}")
	finally:
		frappe.flags.in_workflow = False


def _grain_matches(definition, axes):
	"""A blank Definition axis is a wildcard; a set axis must equal the subject's grain. A non-Lead subject
	(axes all None) matches only a fully-wildcard Definition."""
	for want, got in zip((definition.vertical, definition.group, definition.program), axes, strict=False):
		if want and want != (got or ""):
			return False
	return True


def _start_one(workflow_name, version_name, lead_name, seed_context):
	"""Create the durable Instance for a CONTINUOUS Flow and run its first segment in ONE transaction,
	committing at the first suspend (advance). The Instance's subject is the resolved parent LEAD (D7) — so
	effects act on the lead and the review-signal detector (which looks up Parked instances by CRM Lead) can
	find it — while `seed_context` (the trigger record's own fields) is carried in `state_json`, so a Branch
	or Assign before the first Wait reads real trigger values instead of `{}`. The version is the one
	`_maybe_start` already classified (no re-resolve). The `active_key` UNIQUE index closes the double-start
	race — a second entry for the same (workflow, lead) raises IntegrityError on insert, caught + treated as
	already-running."""
	entry_node = versions.entry_node_of(versions.load(version_name))  # the ONE entry-resolution brain
	frappe.flags.in_workflow = True  # the first segment's own writes must not re-enter entry detection
	try:
		instance = frappe.get_doc({
			"doctype": INSTANCE_DT,
			"workflow": workflow_name,
			"workflow_version": version_name,
			"subject_doctype": "CRM Lead",
			"subject_name": lead_name,
			"current_node": entry_node,
			"state_json": frappe.as_json(seed_context or {}),
			"status": "Running",
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — workflow engine, entry trigger
		interpreter.advance(instance)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback()  # active_key UNIQUE rejected a second live Instance - already running (F3)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="workflow: entry start failed", message=f"workflow={workflow_name} lead={lead_name} :: {frappe.get_traceback()}")
	finally:
		frappe.flags.in_workflow = False
