"""The entry trigger - the ONE seam that starts an Instance, on the wildcard `doc_events["*"]` (the
automation router's proven precedent), guarded by its OWN `frappe.flags.in_workflow` re-entrancy flag so
it coexists with the automation engine's `in_automation` guard and neither engine fires the other.

On the trigger subject's event (Created/Updated/Deleted), every ACTIVE workflow whose grain
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
from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import ARMED_STATE
from tatva_connect.taxonomy import grain
from tatva_connect.workflow_engine import ENGINE_SWITCH, interpreter, registry, versions

INSTANCE_DT = interpreter.INSTANCE_DT
_WORKFLOW_DT = "CRM Workflow"


def _engine_may_run() -> bool:
	"""May the engine act on this save at all?

	THREE gates, and each closes a different door:

	  in_workflow  — re-entrancy. A write the engine itself made must not re-enter its own lane.
	  in_migrate / in_install / in_patch — the schema is being CHANGED underneath us. A patch that saves
	      a document would otherwise fire the dispatcher against a half-migrated table, and the run
	      would either crash the migration or, worse, fire real automation at a customer mid-upgrade.
	      Frappe sets these flags itself; this is its own signal, not a bench workaround.
	  the engine switch — dormant by default. An operator arms it, and until they do nothing runs.
	"""
	if frappe.flags.get("in_workflow"):
		return False
	if frappe.flags.get("in_migrate") or frappe.flags.get("in_install") or frappe.flags.get("in_patch"):
		return False
	return automation.is_enabled(ENGINE_SWITCH)


def on_created(doc, method=None):
	_maybe_start(doc, "Created")


def on_updated(doc, method=None):
	_maybe_start(doc, "Updated")


def on_trash(doc, method=None):
	_maybe_start(doc, "Deleted")


# A Frappe lifecycle event AS a signal source: completing a task the engine raised wakes the run that
# raised it. The task carries the token its node minted, so the wake is PER TASK — the detector used to
# match on the task's lead and one hardcoded `review_done` signal, which meant any Done task of any type
# could wake a journey waiting on a different task of the same lead, and only one flavour of wait was
# expressible at all.
_TASK_OUTCOMES = {"Done": "task.completed", "Completed": "task.completed", "Closed": "task.completed",
                  "Cancelled": "task.cancelled"}


def on_task_done(doc, method=None):
	"""Wildcard `doc_events["*"]["on_update"]`: a CRM Task reaching a terminal status emits its outcome.

	The outcome name comes from the status, and the correlation comes from the task's own workflow token
	— so `deliver_signal` reaches exactly the run and the wait that raised THIS task. A task the engine
	did not raise carries no token and emits nothing. Dormant-by-default and non-re-entrant, and every
	cheap shape check runs before the gates, because this fires on every doctype's update.
	"""
	if doc.doctype != "CRM Task":
		return
	outcome = _TASK_OUTCOMES.get(doc.get("status") or "")
	if not outcome:
		return
	token = doc.get("custom_workflow_token")
	if not token or doc.get("reference_doctype") != "CRM Lead" or not doc.get("reference_docname"):
		return  # not a task this engine raised — nothing correlates to it
	if not _engine_may_run():
		return
	from tatva_connect.workflow_engine import signals

	signals.deliver_signal(
		"CRM Lead", doc.reference_docname, outcome, correlation=token, payload={"status": doc.status}
	)


def run_guards(doc, method=None):
	"""Wildcard `validate` — the SYNCHRONOUS guard lane. Before the save commits, every ACTIVE workflow
	matching this record's (doctype, event) + grain + predicate enforces the REQUIREMENTS declared on its
	Trigger; a handler raising propagates straight out of validate and BLOCKS the save, never swallowed.

	Requirements are declared on the Trigger, alongside the predicate, because both qualify the subject —
	so what a workflow demands before it acts is legible in one place instead of hidden among the actions
	of any node in the graph. Guard handlers themselves are the automation engine's, reused not copied.
	Dormant-by-default and non-re-entrant, so a write the engine made never re-enters its own guard lane."""
	if not _engine_may_run():
		return
	ctx = _trigger_context(doc, "Created" if doc.is_new() else "Updated")
	if ctx is None:
		return
	from tatva_connect.automation import actions

	for version_name in ctx.versions:
		version = versions.load(version_name)
		if not _predicate_holds(version, ctx):
			continue  # the predicate did not hold — this workflow does not judge this save
		for requirement in _requirements(version):
			verb = requirement.get("verb")
			if actions.lane_of(verb) != "guard":
				continue  # only guard verbs may be requirements; the node validator enforces it at author time
			handler = actions.handler_of(verb)
			params = frappe._dict(requirement.get("params") or {})
			params.action_type = verb
			handler(params, ctx.subject, ctx.context)  # a raise here IS the block (reaches validate unswallowed)


def covering_location_guard(doc):
	"""True iff an ACTIVE Flow with a Require Location guard already covers THIS save — its guard lane
	ran (or will run) synchronously in the same validate. The location backstop (tasks.enforce_location)
	reads this to STAND DOWN instead of double-guarding: the Flow-era replacement for the old
	"does a Require Location rule cover this?" check the rule engine used. Non-re-entrant + dormant like
	the guard lane itself."""
	if not _engine_may_run():
		return False
	ctx = _trigger_context(doc, "Created" if doc.is_new() else "Updated")
	if ctx is None:
		return False
	for version_name in ctx.versions:
		version = versions.load(version_name)
		if not _predicate_holds(version, ctx):
			continue
		if any(r.get("verb") == "Require Location" for r in _requirements(version)):
			return True
	return False


def _maybe_start(doc, event):
	"""The after-save lane: run every ACTIVE workflow whose Trigger (subject, event) + grain + predicate match
	this write. A wait-free Flow runs inline and persists nothing (EPHEMERAL, D4); a Flow that parks starts
	a durable Instance (CONTINUOUS). Guard-lane actions already ran (or blocked the save) in `run_guards`."""
	if not _engine_may_run():
		return
	ctx = _trigger_context(doc, event)
	if ctx is None:
		return
	for version_name in ctx.versions:
		version = versions.load(version_name)
		if not _predicate_holds(version, ctx):
			continue  # the When did not hold — this Flow does not act on this write
		if interpreter.has_wait(version):
			# The triggering record travels with the run: the subject is always the parent lead, so without
			# this a node configured to act on the trigger doc silently acted on the lead instead.
			_enqueue_start(version.workflow, version_name, ctx.subject, _run_seed(ctx.context), (doc.doctype, doc.name))
		else:
			_run_ephemeral(version_name, ctx.subject, doc, ctx.context)  # EPHEMERAL: run inline, persist nothing


def _trigger_context(doc, event):
	"""Shared setup for both Flow lanes (guard + effect), reusing the automation engine's ONE brains. The
	cheap Definition query gates everything, so an unrelated save resolves no subject and builds no context.
	Returns `_dict(subject, context, field_types, versions)` — the resolved parent LEAD (the effect verbs'
	subject, D7), the trigger context the When reads (with `{field}__before` for an Updated diff), the
	field-type map for type-aware criteria, and the current frozen version of each grain-matched Flow — or
	`None` when nothing can match (fail-closed)."""
	# One indexed query on the derived trigger columns — why they are materialised off the Trigger.
	workflows = frappe.get_all(
		_WORKFLOW_DT,
		filters={"lifecycle_state": ARMED_STATE, "trigger_doctype": doc.doctype, "trigger_event": event},
		fields=["name", "trigger_vertical as vertical", "trigger_group as group", "trigger_program as program"],
	)
	if not workflows:
		return None
	from tatva_connect.automation import context as ctx_build

	subject = ctx_build.subject(doc)
	if subject is None:
		return None  # no resolvable parent lead → no Flow can act (fail-closed)
	axes = ctx_build.subject_axes(subject)
	matched = [w for w in workflows if grain.covers(w, *axes)]
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


def _trigger_config(version):
	"""What this workflow's Trigger declares, read out of the FROZEN version. The ONE reader.

	Everything that qualifies a subject — the predicate and the requirements — is declared on the Trigger,
	so a Run already under way is judged by the terms it started under and editing the workflow cannot
	reach back. Both callers below come through here; neither goes looking for the Trigger itself.
	"""
	trigger = next((n for n in version.nodes if n.get("node_type") == registry.TRIGGER), None)
	if trigger is None:
		return None  # no trigger, nothing to qualify against
	return frappe.parse_json(trigger.get("config_json") or "{}") or {}


def _requirements(version):
	"""The Requirements declared on this workflow's Trigger — the guard-lane verbs a save must satisfy.

	A requirement says "this lead needs a phone number", "this task needs a location", and it is declared
	on the Trigger because it qualifies the SUBJECT exactly as the predicate does. The lane used to scan
	every node of every matching workflow for guard verbs among its actions, which let a guard hide
	anywhere in a graph — an author could not tell by looking what a workflow demanded before it acted.
	"""
	return (_trigger_config(version) or {}).get("requirements") or []


def _predicate_holds(version, ctx):
	"""Does this workflow's Trigger predicate qualify the subject? No predicate means an open gate."""
	config = _trigger_config(version)
	if config is None:
		return False  # fail closed
	return rules.predicate_match(config.get("predicate"), ctx.context, ctx.field_types)


def _enqueue_start(workflow_name, version_name, lead_name, seed_context, trigger_ref=None):
	"""Start a durable run AFTER the triggering save commits — never inside the user's transaction.

	This used to call `_start_one` inline, and that was a data-loss bug rather than a style one. A
	doc_event runs inside the caller's transaction, so the engine's own `frappe.db.commit()` committed
	the USER'S whole pending write, and its `frappe.db.rollback()` on the failure path DISCARDED the
	record the user had just saved — while the request still returned success. A rep pressed Save, saw it
	work, and the lead was gone.

	Enqueued `after_commit`, so the job owns its own transaction and may commit and roll back freely. The
	same shape `deliver_signal` already uses. Deduplicated per (workflow, lead): several saves inside one
	request must not queue several starts, and the `active_key` unique index is the second line of
	defence behind this one.
	"""
	frappe.enqueue(
		"tatva_connect.workflow_engine.triggers.start_run",
		queue="short",
		enqueue_after_commit=True,
		now=bool(frappe.flags.get("in_test")),
		job_id=f"workflow-start::{workflow_name}::{lead_name}",
		deduplicate=True,
		workflow_name=workflow_name,
		version_name=version_name,
		lead_name=lead_name,
		seed_context=seed_context,
		trigger_ref=list(trigger_ref) if trigger_ref else None,
	)


def start_run(workflow_name, version_name, lead_name, seed_context=None, trigger_ref=None):
	"""The queued entry point. Re-checks the gate, because the switch may have been turned off between
	the save and the job running, and a job that starts a run the operator has disarmed is exactly the
	kind of thing dormant-by-default exists to prevent."""
	if not automation.is_enabled(ENGINE_SWITCH):
		return
	_start_one(workflow_name, version_name, lead_name, seed_context, trigger_ref)


def _run_seed(context):
	"""The part of the trigger context worth STORING on the run, in `state_json`'s nested-by-writer shape.

	A document's fields belong to the document. The only thing here that cannot be read back later is the
	before/after pair the `changed to` operators need, so that is all a run carries forward — and it stays
	in the record's OWN namespace, because a before-value is a value of that record. Nothing shadows the
	live document by doing so: no doctype has a `<field>__before` column.

	Computed BEFORE the enqueue, not inside the job, because a `refs.Values` resolves off live documents
	and must never be serialised through a queue. Only this plain dict crosses.
	"""
	from tatva_connect.workflow_engine import refs

	buckets = context.buckets if isinstance(context, refs.Values) else (context or {})
	seeded = {
		source: {k: v for k, v in bucket.items() if k.endswith(refs.BEFORE)}
		for source, bucket in buckets.items()
	}
	return {source: bucket for source, bucket in seeded.items() if bucket}


def _start_one(workflow_name, version_name, lead_name, seed_context, trigger_ref=None):
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
			# What actually fired, kept apart from the subject. A Task save and a lead save both resolve to
			# the same lead, and a node acting "on the trigger doc" means different records in each case.
			"trigger_doctype": trigger_ref[0] if trigger_ref else None,
			"trigger_name": trigger_ref[1] if trigger_ref else None,
			"current_node": entry_node,
			# Only what a later segment cannot re-derive. The subject's own fields are NOT seeded: they
			# live on the document and are read from it each segment, so copying them here would freeze
			# the lead as it was at trigger time — which is what made a 30-day Wait test 30-day-old data.
			# The `__before` pairs are kept, because the change that fired this run is not re-derivable.
			"state_json": frappe.as_json(seed_context or {}),
			"status": "Running",
			# The uniqueness key the double-start guard rests on. It was DECLARED on the doctype and
			# described in three docstrings, but the column was not unique and nothing ever wrote it — so
			# every save of a matching lead started another journey, and each one sent its own messages.
			# Cleared when the run reaches a terminal state, so the same lead may enter again later.
			"active_key": f"{workflow_name}::{lead_name}",
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — workflow engine, entry trigger
		interpreter.advance(instance)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback()  # active_key UNIQUE rejected a second live Instance - already running (F3)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="workflow: entry start failed", message=f"workflow={workflow_name} lead={lead_name} :: {frappe.get_traceback()}")
	finally:
		frappe.flags.in_workflow = False
