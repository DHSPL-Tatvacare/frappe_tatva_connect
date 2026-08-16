"""The entry trigger - the ONE seam that starts an Journey, on the wildcard `doc_events["*"]` (the
automation router's proven precedent), guarded by its OWN `frappe.flags.in_workflow` re-entrancy flag so
it coexists with the automation engine's `in_automation` guard and neither engine fires the other.

On the trigger subject's event (Created/Updated/Deleted), every ACTIVE workflow whose grain
matches the subject starts: an Journey is created AND its first segment runs in ONE transaction,
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
from tatva_connect.propagate import fail_safe
from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import ARMED_STATE
from tatva_connect.taxonomy import grain
from tatva_connect.workflow_engine import ENGINE_SWITCH, interpreter, registry, versions

JOURNEY_DT = interpreter.JOURNEY_DT
_WORKFLOW_DT = "CRM Workflow"


def _may_touch_journeys() -> bool:
	"""Is it structurally safe for this lane to act on this save? Two gates, each closing a door:

	  in_workflow  — re-entrancy. A write the engine itself made must not re-enter its own lane.
	  in_migrate / in_install / in_patch — the schema is being CHANGED underneath us. A patch that saves
	      a document would otherwise fire the dispatcher against a half-migrated table, and the journey
	      would either crash the migration or, worse, fire real automation at a customer mid-upgrade.
	      Frappe sets these flags itself; this is its own signal, not a bench workaround.

	Neither is a dormancy setting, which is why ENDING a journey passes this and not the switch below.
	"""
	if frappe.flags.get("in_workflow"):
		return False
	return not (frappe.flags.get("in_migrate") or frappe.flags.get("in_install") or frappe.flags.get("in_patch"))


def _engine_may_run() -> bool:
	"""May the engine ADVANCE anything on this save? The gates above, plus the switch.

	THE RULE: the engine switch gates what makes a journey advance. It never gates ending one. Dormant by
	default — an operator arms the switch, and until they do nothing starts, resumes or steps forward.
	"""
	return _may_touch_journeys() and automation.is_enabled(ENGINE_SWITCH)


# PROPAGATE (@fail_safe): these ride the WILDCARD, so an engine fault here breaks every save site-wide; a lost start is re-startable through the SAME `start_journey` the cohort drain uses, and `active_key` stops a double-run.
@fail_safe
def on_created(doc, method=None):
	_maybe_start(doc, "Created")


@fail_safe
def on_updated(doc, method=None):
	_maybe_start(doc, "Updated")


@fail_safe
def on_trash(doc, method=None):
	_maybe_start(doc, "Deleted")


# The two triggers of ONE behaviour — `interpreter.stop_for_subject`. Deliberately NOT @fail_safe: a lost stop leaves a journey parked on a lead that is gone, which is the exact defect this closes, and nothing rebuilds it.
def on_lead_deleted(doc, method=None):
	"""CRM Lead.on_trash — the lead is going, so every journey about it ends with it.

	Runs BEFORE the wildcard `on_trash` above (frappe composes `doc_events[doctype] + doc_events["*"]`,
	`document.py:1598`), so a Deleted-entry workflow starting on this same delete is not stopped by it.

	NOT gated on the engine switch — ending a journey is cleanup, and one parked on a lead the database
	has forgotten is neither stopped nor gone whether or not an operator has armed anything.
	"""
	if _may_touch_journeys():
		interpreter.stop_for_subject(doc.doctype, doc.name, f"Lead deleted ({doc.name})")


def on_lead_grain_changed(doc, method=None):
	"""CRM Lead.on_update — a lead that changed grain is a different patient population.

	Stopped and gone, never re-routed: the lead is picked up by the new grain's workflows the same way
	enrolment already works, and a journey frozen against the grain it no longer has must not carry on.
	The axes are read from the schema (`grain.columns`), never restated here.

	The guard is `get_doc_before_save()`, NOT `is_new()`/`has_value_changed()`: `has_value_changed` returns
	True for EVERY field when there is no before-image (`document.py:684`), and `is_new()` is already False
	by the time `on_update` runs inside an insert — so that pair reads a lead's CREATION as a grain change
	and stopped the journey the same save had just started. Caught by `test_entry_isolation`, not by
	reasoning about it.

	Not gated on the engine switch either — one behaviour, one answer about the switch.
	"""
	before = doc.get_doc_before_save()
	if not before or not _may_touch_journeys():
		return
	moved = [axis for axis in grain.columns("CRM Lead") if axis and before.get(axis) != doc.get(axis)]
	if moved:
		interpreter.stop_for_subject(doc.doctype, doc.name, f"Lead grain changed ({', '.join(moved)})")


# A Frappe lifecycle event AS a signal source: completing a task the engine raised wakes the journey that
# raised it. The task carries the token its node minted, so the wake is PER TASK — the detector used to
# match on the task's lead and one hardcoded `review_done` signal, which meant any Done task of any type
# could wake a journey waiting on a different task of the same lead, and only one flavour of wait was
# expressible at all.
_TASK_OUTCOMES = {"Done": "task.completed", "Completed": "task.completed", "Closed": "task.completed",
                  "Cancelled": "task.cancelled"}


# PROPAGATE (@fail_safe): a lost emission leaves the journey Parked exactly where it was, and this fires on
# EVERY update of a terminal-status task — so the next save of that task re-emits the same correlated signal.
@fail_safe
def on_task_done(doc, method=None):
	"""Wildcard `doc_events["*"]["on_update"]`: a CRM Task reaching a terminal status emits its outcome.

	The outcome name comes from the status, and the correlation comes from the task's own workflow token
	— so `deliver_signal` reaches exactly the journey and the wait that raised THIS task. A task the engine
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


def _maybe_start(doc, event):
	"""The after-save lane: run every ACTIVE workflow whose Trigger (subject, event) + grain + predicate match
	this write. A wait-free Flow runs inline and persists nothing (EPHEMERAL, D4); a Flow that parks starts
	a durable Journey (CONTINUOUS). A workflow never runs before the save and never blocks it (Phase 11)."""
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
			# The triggering record travels with the journey: the subject is always the parent lead, so without
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
		# Both records in both halves; the lead doc is the one `subject()` already loaded for the grain — no new read.
		context=ctx_build.context_for(doc, changed, lead=subject),
		field_types=ctx_build.field_types_for(doc.doctype, "CRM Lead"),
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
		frappe.log_error(title="workflow: ephemeral journey failed", message=f"version={version_name} subject={lead_name} :: {frappe.get_traceback()}")
	finally:
		frappe.flags.in_workflow = False


def _trigger_config(version):
	"""What this workflow's Trigger declares, read out of the FROZEN version. The ONE reader.

	Everything that qualifies a subject — the predicate and the requirements — is declared on the Trigger,
	so a journey already under way is judged by the terms it started under and editing the workflow cannot
	reach back. Both callers below come through here; neither goes looking for the Trigger itself.
	"""
	trigger = next((n for n in version.nodes if n.get("node_type") == registry.TRIGGER), None)
	if trigger is None:
		return None  # no trigger, nothing to qualify against
	return registry.config_of(trigger)


def _predicate_holds(version, ctx):
	"""Does this workflow's Trigger predicate qualify the subject? No predicate means an open gate."""
	config = _trigger_config(version)
	if config is None:
		return False  # fail closed
	return rules.predicate_match(config.get("predicate"), ctx.context, ctx.field_types)


def _enqueue_start(workflow_name, version_name, lead_name, seed_context, trigger_ref=None):
	"""Start a durable journey AFTER the triggering save commits — never inside the user's transaction.

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
		"tatva_connect.workflow_engine.triggers.start_journey",
		# The workflow lane, its own worker: on `short` a burst of starts starves `wakeups.sweep`, which is the reconciler that rescues runs whose wake was lost.
		queue="workflow",
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


def start_journey(workflow_name, version_name, lead_name, seed_context=None, trigger_ref=None):
	"""The queued entry point. Re-checks the gate, because the switch may have been turned off between
	the save and the job running, and a job that starts a journey the operator has disarmed is exactly the
	kind of thing dormant-by-default exists to prevent.

	Returns the run-once REFUSAL when there is one, and None when the journey was started — so a caller
	that wants to say why a patient was skipped has the reason rather than a silent absence.
	"""
	if not automation.is_enabled(ENGINE_SWITCH):
		return None
	refusal = _already_ran(workflow_name, version_name, lead_name)
	if refusal:
		return refusal
	_start_one(workflow_name, version_name, lead_name, seed_context, trigger_ref)
	return None


def _already_ran(workflow_name, version_name, lead_name):
	"""W8.4 — has this workflow already completed for this lead? The reason if so, None if not.

	ONE CHECK, AT THE ONE DOOR. Both lanes arrive here — a save through `_enqueue_start`, a cohort through
	`drain._start_one` — so the question is asked once and cannot drift between them. The cohort is where
	it matters: it re-selects everyone its criteria match on every tick.

	ANY VERSION COUNTS, so the filter names the WORKFLOW and never `workflow_version`. A version is an
	edit and a workflow is an identity; keying on the version would let a typo fix re-admit every lead who
	ever finished.

	`Done` ONLY. `Failed` is the engine breaking and must not cost the patient their journey; `Stopped` is
	an operator suspending the workflow, and barring everyone who was in flight because of one button is
	not what that button means.

	IT DOES NOT READ `active_key`. That key is cleared on every terminal transition — that is precisely how
	W10's kill frees a lead to enter again — so a check built on it would answer "never ran" for every
	completed lead. Two different questions, and conflating them breaks Suspend.
	"""
	config = _trigger_config(versions.load(version_name)) or {}
	if not config.get("once_per_subject"):
		return None
	# `creation` is when the journey began, which is what "already completed" is dated by for a reader.
	done = frappe.db.get_value(
		JOURNEY_DT,
		{"workflow": workflow_name, "subject_doctype": "CRM Lead", "subject_name": lead_name,
		 "status": interpreter.DONE},
		"creation",
		order_by="creation desc",
	)
	if not done:
		return None
	return f"Already completed on {frappe.utils.formatdate(done)}"


_SUBJECT_SLUG = frappe.scrub("CRM Lead")


def _run_seed(context):
	"""The part of the trigger context worth STORING on the journey, in `state_json`'s nested-by-writer shape.

	The subject (the lead, `crm_lead`) is stripped to `__before` pairs only — its live values are
	replaced by a record loader each segment, and copying them would freeze the lead as it was at
	trigger time (a 30-day Wait would see 30-day-old data).

	Every OTHER bucket — the trigger doc itself (crm_task, file, whatsapp_message) — is kept in full.
	It has no live loader, and the author explicitly chose fields FROM that trigger record. Stripping
	them silently defaulted due dates and blanked subjects on the durable path while the ephemeral path
	carried them correctly. A trigger-task snapshot at trigger time IS the value the author asked for;
	it is not stale — it is the defined reference point.

	Computed BEFORE the enqueue, not inside the job, because a `refs.Values` resolves off live documents
	and must never be serialised through a queue. Only this plain dict crosses.
	"""
	from tatva_connect.workflow_engine import refs

	buckets = context.buckets if isinstance(context, refs.Values) else (context or {})
	seeded = {}
	for source, bucket in buckets.items():
		if source == _SUBJECT_SLUG:
			kept = {k: v for k, v in bucket.items() if k.endswith(refs.BEFORE)}
		else:
			kept = dict(bucket)
		if kept:
			seeded[source] = kept
	return seeded


def _start_one(workflow_name, version_name, lead_name, seed_context, trigger_ref=None):
	"""Create the durable Journey for a CONTINUOUS Flow and run its first segment in ONE transaction,
	committing at the first suspend (advance). The Journey's subject is the resolved parent LEAD (D7) — so
	effects act on the lead and the review-signal detector (which looks up Parked journeys by CRM Lead) can
	find it — while `seed_context` (the trigger record's own fields) is carried in `state_json`, so a Route
	or Assign before the first Wait reads real trigger values instead of `{}`. The version is the one
	`_maybe_start` already classified (no re-resolve). The `active_key` UNIQUE index closes the double-start
	race — a second entry for the same (workflow, lead) raises IntegrityError on insert, caught + treated as
	already-running."""
	frappe.flags.in_workflow = True  # the first segment's own writes must not re-enter entry detection
	try:
		# `active_key` is the double-start guard and the durable lane's alone; cleared on every terminal transition.
		journey = interpreter.open_journey(
			workflow_name, version_name, lead_name, seed_context, trigger_ref,
			active_key=f"{workflow_name}::{lead_name}",
		)
		interpreter.advance(journey)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback()  # active_key UNIQUE rejected a second live Journey - already running (F3)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="workflow: entry start failed", message=f"workflow={workflow_name} lead={lead_name} :: {frappe.get_traceback()}")
	finally:
		frappe.flags.in_workflow = False
