"""The entry trigger: the one seam that starts a Journey from the wildcard `doc_events`, dormant until the engine switch is on."""
import frappe
from frappe.cache_manager import get_doctype_map

from tatva_connect import automation, utils
from tatva_connect.automation import rules
from tatva_connect.propagate import fail_safe
from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import ARMED_STATE
from tatva_connect.taxonomy import grain
from tatva_connect.workflow_engine import ENGINE_SWITCH, as_system, interpreter, registry, versions

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


def _watched(event):
	"""The wildcard hooks' first test: the engine may run, and some Active workflow is triggered by this doctype and event (cached reads)."""
	return lambda doc: _engine_may_run() and bool(active_triggers(doc.doctype, event))


# PROPAGATE (@fail_safe): these ride the WILDCARD, so an engine fault here breaks every save site-wide; a lost start is re-startable through the SAME `start_journey` the cohort drain uses, and `active_key` stops a double-run.
# `on_created` rides `on_update`, which also fires on insert; `in_insert` keeps "Created" meaning created, after frappe's Assignment Rule has set the owner.
@fail_safe(applies=lambda doc: doc.flags.in_insert and _watched("Created")(doc))
def on_created(doc, method=None):
	_maybe_start(doc, "Created")


@fail_safe(applies=_watched("Updated"))
def on_updated(doc, method=None):
	_maybe_start(doc, "Updated")


@fail_safe(applies=_watched("Deleted"))
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


# PROPAGATE (@fail_safe): a lost emission leaves the journey Parked exactly where it was, and the signal is the TRANSITION into a terminal status, so a task saved again while Done emits nothing.
@fail_safe(applies=lambda doc: doc.doctype == "CRM Task" and doc.has_value_changed("status"))
def on_task_done(doc, method=None):
	"""Wildcard `doc_events["*"]["on_update"]`: a CRM Task reaching a terminal status emits its outcome.

	The outcome name comes from the status, and the correlation comes from the task's own workflow token
	— so `deliver_signal` reaches exactly the journey and the wait that raised THIS task. A task the engine
	did not raise carries no token and emits nothing. Dormant-by-default and non-re-entrant, and every
	cheap shape check runs before the gates, because this fires on every doctype's update.
	"""
	# `applies` admits only a CRM Task whose status moved: the TRANSITION emits, not the state, so a re-save of a Done task enqueues nothing.
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
	this write. A Flow that can finish inside the save runs inline and persists nothing (EPHEMERAL); one that
	parks or needs its own transaction starts a durable Journey (CONTINUOUS), which `must_be_durable` decides.
	A workflow never runs before the save and never blocks it (Phase 11)."""
	if not _engine_may_run():
		return
	ctx = _trigger_context(doc, event)
	if ctx is None:
		return
	for version_name in ctx.versions:
		version = versions.load(version_name)
		if not _admits(version, ctx):
			continue  # no lead and not asked to run without one, or the When did not hold — this Flow does not act on this write
		if interpreter.must_be_durable(version):
			# The triggering record travels with the journey, so a node set to act on it never acts on the lead instead.
			_book_start(version.workflow, version_name, ctx.subject, _run_seed(ctx.context), (doc.doctype, doc.name), _trigger_config(version))
		elif not _already_ran(version.workflow, ctx.subject, _trigger_config(version), (doc.doctype, doc.name)):
			# "Only once per patient" holds inline too: the one run-once check, asked of the inline lane as of the durable one.
			_run_ephemeral(version_name, ctx.subject, doc, ctx.context, ctx.axes)  # EPHEMERAL: run inline, persist nothing


def _trigger_context(doc, event):
	"""Shared setup for both Flow lanes (guard + effect), reusing the automation engine's ONE brains. The
	cheap Definition query gates everything, so an unrelated save resolves no subject and builds no context.
	Returns `_dict(subject, context, fields, versions)` — the resolved parent LEAD (the effect verbs'
	subject, D7), the trigger context the When reads (with `{field}__before` for an Updated diff), the
	field-type map for type-aware criteria, and the current frozen version of each grain-matched Flow — or
	`None` when nothing can match (fail-closed)."""
	workflows = active_triggers(doc.doctype, event)
	if not workflows:
		return None
	from tatva_connect.automation import context as ctx_build
	from tatva_connect.automation import subjects

	# The grain decides on three columns; the whole lead is loaded only once some workflow covers it.
	lead_name = doc.name if doc.doctype == "CRM Lead" else subjects.resolve_lead_name(doc)
	axes = ctx_build.subject_axes(doc) if doc.doctype == "CRM Lead" else (lead_name and ctx_build.lead_axes(lead_name))
	if not axes:
		lead_name, axes = None, subjects.own_axes(doc)  # no lead: the grain the record names for itself, taken only by a Trigger that also runs without one
	if not axes:
		return None  # neither a lead nor a grain of its own → no Flow can act (fail-closed)
	covered = [w.name for w in workflows if grain.covers(w, *axes)]
	# The cached list can lag a lifecycle move by a moment across workers, so what may start is re-read from the table.
	matched = frappe.get_all(_WORKFLOW_DT, filters={"name": ("in", covered), "lifecycle_state": ARMED_STATE}, pluck="name") if covered else []
	if not matched:
		return None
	subject = ctx_build.subject(doc) if lead_name else None
	if lead_name and subject is None:
		return None
	changed = ctx_build.diff_watched_fields(doc) if event == "Updated" else {}
	return frappe._dict(
		subject=subject.name if subject else None,
		axes=axes,
		# Both records in both halves; the lead doc is the one `subject()` already loaded for the grain — no new read.
		context=ctx_build.context_for(doc, changed, lead=subject),
		fields=ctx_build.fields_for(doc.doctype, "CRM Lead"),
		versions=versions.current_names(matched),
	)


def active_triggers(doctype, event):
	"""Active workflows triggered by this doctype and event, with their grain — Frappe's own cached doctype map (as Assignment Rules use), cleared by every workflow save; the grain columns come from the document cache."""
	found = []
	for row in get_doctype_map(_WORKFLOW_DT, f"{doctype}::{event}", filters={"lifecycle_state": ARMED_STATE, "trigger_doctype": doctype, "trigger_event": event}):
		axes = frappe.get_cached_value(_WORKFLOW_DT, row.name, ["trigger_vertical", "trigger_group", "trigger_program"])
		if axes:  # a workflow deleted since the list was cached simply drops out
			found.append(frappe._dict(name=row.name, vertical=axes[0], group=axes[1], program=axes[2]))
	return found


def _run_ephemeral(version_name, lead_name, trigger_doc, context, axes=None):
	"""Run an inline-eligible Flow inside the triggering save. An ephemeral effect can DO but never DENY:
	`run_inline`'s savepoint isolates its writes and a failure is logged, not propagated, so the save is untouched.
	`TRANSACTION_LOST` is the one that must propagate - the save went with it, so swallowing it would report a
	record that was never written. `in_workflow` guards the effects' own writes from re-entering the front-door."""
	frappe.flags.in_workflow = True
	try:
		interpreter.run_inline(version_name, lead_name, trigger_doc, context, axes=axes)
	except utils.TRANSACTION_LOST:
		raise
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


def _admits(version, ctx):
	"""Does this workflow's Trigger take this write? A record no lead stands behind only when the Trigger also runs without one, then its predicate; no predicate means an open gate."""
	config = _trigger_config(version)
	if config is None:
		return False  # fail closed
	if ctx.subject is None and not config.get("without_lead"):
		return False
	return rules.predicate_match(config.get("predicate"), ctx.context, ctx.fields)


def _book_start(workflow_name, version_name, lead_name, seed_context, trigger_ref=None, config=None):
	"""Book a durable start: the journey parks at its entry, due now, and the paced drain's `wake_due` starts it. Returns the run-once refusal, else None."""
	from tatva_connect.workflow_engine import drain

	config, due = config or {}, frappe.utils.now_datetime()
	save_point = f"tc_wf_book_{frappe.generate_hash(length=8)}"  # a refused or duplicate start must never fail the save
	frappe.db.savepoint(save_point)
	frappe.flags.in_workflow = True  # the journey's own insert must not re-enter entry detection
	try:
		admitted = _gate(workflow_name, lead_name, trigger_ref, config)
		if admitted is not True:
			return admitted
		interpreter.open_journey(workflow_name, version_name, lead_name, seed_context, trigger_ref,
		                         active_key=_active_key(workflow_name, lead_name, trigger_ref, config), due=due)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		frappe.db.rollback(save_point=save_point)  # active_key UNIQUE: a live journey already holds this run (F3)
		return
	finally:
		frappe.flags.in_workflow = False
	drain.pull_forward(due)  # as a park does: booked after commit, never pushing an earlier pass back


def start_journey(workflow_name, version_name, lead_name, seed_context=None, trigger_ref=None):
	"""The job-side entry, for a cohort lead and a start job queued before the drain took starts: books the start as a save does, then commits.

	Re-checks the switch, because it may have been turned off since the work was queued. Returns the run-once
	refusal when there is one, and None otherwise, so a caller that wants to say why a patient was skipped has the reason.
	"""
	as_system()
	utils.read_committed()
	if not automation.is_enabled(ENGINE_SWITCH):
		return None
	config = _trigger_config(versions.load(version_name)) or {}
	refusal = _book_start(workflow_name, version_name, lead_name, seed_context, trigger_ref, config)
	frappe.db.commit()
	return refusal


def _gate(workflow_name, lead_name, trigger_ref, config):
	"""THE start gate, for a save and a cohort alike, read-only: True to start, the run-once refusal, or None when a live run already covers it."""
	refusal = _already_ran(workflow_name, lead_name, config, trigger_ref)
	if refusal:
		return refusal
	if _still_running(workflow_name, lead_name, trigger_ref, config):
		return None
	return True


def clear_to_start(journey):
	"""A booked Restart run ends the lead's older waiting runs of its workflow in the drain's own transaction; False while one is mid-step, so it waits a pass."""
	version = versions.load(journey.workflow_version)
	if journey.current_node != versions.entry_node_of(version) or not journey.trigger_name:
		return True  # not a booked start, or not one a record fired
	if (_trigger_config(version) or {}).get("retrigger") != registry.RETRIGGER_RESTART:
		return True
	older = {"workflow": journey.workflow, "name": ["!=", journey.name], "creation": ["<", journey.creation]}
	interpreter.stop_for_subject(journey.subject_doctype, journey.subject_name, f"Restarted by {journey.trigger_doctype} {journey.trigger_name}",
	                             within={**older, "status": "Parked"}, skip_locked=True)
	return not frappe.db.exists(JOURNEY_DT, {**older, "subject_doctype": journey.subject_doctype,
	                                         "subject_name": journey.subject_name, "status": ["in", interpreter.LIVE_STATES]})


def _active_key(workflow_name, lead_name, trigger_ref, config):
	"""The double-start guard a live journey holds, per record whenever the Trigger acts on each new one; cleared on every terminal transition."""
	return _run_key(workflow_name, lead_name, trigger_ref, per_record=bool((config or {}).get("retrigger")))


def _still_running(workflow_name, lead_name, trigger_ref, config):
	"""A live run this start would repeat, read off the rows so a run keyed under an earlier setting still counts: any of the lead's, or this record's once the Trigger acts on each new record."""
	subject_doctype, subject_name = interpreter.subject_of(lead_name, trigger_ref)
	live = {"workflow": workflow_name, "subject_doctype": subject_doctype, "subject_name": subject_name, "status": ["in", interpreter.LIVE_STATES]}
	if trigger_ref and config.get("retrigger"):
		live.update(trigger_doctype=trigger_ref[0], trigger_name=trigger_ref[1])
	return bool(frappe.db.exists(JOURNEY_DT, live))


def _run_key(workflow_name, lead_name, trigger_ref=None, per_record=False):
	"""What one live run, or its start job, is unique on: its subject, or the triggering record too."""
	subject_name = interpreter.subject_of(lead_name, trigger_ref)[1]
	if trigger_ref and per_record:
		return f"{workflow_name}::{subject_name}::{trigger_ref[0]}:{trigger_ref[1]}"
	return f"{workflow_name}::{subject_name}"


def _already_ran(workflow_name, lead_name, config, trigger_ref=None):
	"""W8.4 — has this workflow already completed for this lead? The reason if so, None if not.

	ONE CHECK, AT THE ONE DOOR. Both lanes arrive here — a save through `_book_start`, a cohort through
	`cohort._start_one` — so the question is asked once and cannot drift between them. The cohort is where
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
	if not config.get("once_per_subject"):
		return None
	subject_doctype, subject_name = interpreter.subject_of(lead_name, trigger_ref)
	# `creation` is when the journey began, which is what "already completed" is dated by for a reader.
	done = frappe.db.get_value(
		JOURNEY_DT,
		{"workflow": workflow_name, "subject_doctype": subject_doctype, "subject_name": subject_name,
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
