# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W7.2 PART B — the drain that walks a cohort. ONE chunk per job, a keyset cursor, committed per chunk.

IT PACES ITSELF, and that is the whole of its rate: a job starts `DRAIN_CHUNK` journeys, books its own
successor `DRAIN_INTERVAL_SECONDS` later, and exits. Nothing outside contributes to the throughput and
nothing outside has to run for the cohort to finish — `sweep` NOTICES a booking that never arrived, which
is a backstop, not a motor. Turn the scheduler off and a walk in flight still completes.

Each job living for seconds rather than minutes is the second half of that: the lane is free between
chunks, so a wake, a send or a Suspend never queues behind a cohort, and no job ever nears its timeout.

A COHORT IS A journey FACTORY, NOT A SECOND ENGINE. The schedule fires, this walks the leads the Trigger's
criteria select, and each one gets its OWN ordinary journey through `triggers.start_journey` — the SAME entry the
record-event lane calls. Nothing about node contracts, park/resume or the interpreter changes, and there
is no recipient-set inside a journey: that is the evals model and it is deliberately not ours.

WHY ONE JOB AND NOT ONE PER LEAD. `frappe.enqueue` THROWS above `MAX_QUEUED_JOBS = 500`. A 2,000-lead
cohort turned into 2,000 jobs errors partway and leaves the rest of the cohort silently unstarted — the
worst shape available, because it looks like it worked. So the sweep enqueues ONE drain per due workflow
and the drain does the walking.

WHY KEYSET AND NOT OFFSET. `OFFSET 10000` re-reads ten thousand rows to skip them, and rows shift between
pages while the table is written to underneath — so a lead is started twice or never. The cursor is the
lead name, ascending, stored as it goes, and a resume asks for `name > cursor`.

WHAT IT REUSES, SO THERE IS NO SECOND BRAIN:
  * the criteria     — `cohort.matching_leads`, the same grain + `rules.predicate_match` the preview counts with
  * the start        — `triggers.start_journey`, the same entry a save uses; its `active_key` UNIQUE index is
                       what makes a resume unable to double-start a lead even if the cursor were lost
  * the waiting      — `wakeups.schedule_on_lane`, the same booking a parked journey makes
  * the drain shape  — `api.partner_bulk_worker`: claim the row, chunk, commit each, re-read the abort
                       flag between chunks, resume from stored state. Same moves, same order.

DORMANT. `Workflow::Cohort::drain` ships OFF, and both the sweep and the job re-read it — the job because
the switch may be turned off between the two, which is exactly what `triggers.start_journey` guards against.
"""
import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect.automation import settings as automation
from tatva_connect.workflow_engine import cohort, thresholds, wakeups

SWITCH_COHORT = "Workflow::Cohort::drain"

_WORKFLOW_DT = "CRM Workflow"
_PACE_DT = "CRM Cohort Pace Settings"
_RUN_COHORT = "tatva_connect.workflow_engine.drain.run_cohort"

# W4.3 — every number this drain paces itself by is DECLARED in `thresholds`, never re-stated here.

IDLE, DRAINING = "", "Draining"


def _booking_key(workflow_name):
	"""The id this cohort's next chunk is held under — ONE definition, so the sweep's enqueue and the drain's own booking dedupe against each other rather than both running."""
	return f"cohort-drain::{workflow_name}"


def sweep(respect_switch=True):
	"""The scheduled tick: start a drain for every workflow whose cohort is due. Returns how many.

	FRAPPE NATIVE, and the API rejected is named: this is a `scheduler_events` cron entry, not a
	`Scheduled Job Type` row per workflow. A Scheduled Job Type is keyed by METHOD PATH and is site-level,
	so a per-workflow schedule would mean a row per workflow inside frappe's own scheduler loop, where our
	switch, our grain and our abort flag cannot reach it. One tick asking one indexed question is smaller
	and stays ours.

	THE CLAIM AND THE ENQUEUE SHARE ONE COMMIT, and that is why the commit is HERE rather than inside
	`_claim`. `enqueue_after_commit=True` does not enqueue — it registers the call on `frappe.db.after_commit`
	and returns (`background_jobs.py:205`), so it fires on the NEXT commit. With `_claim` committing first,
	each deferred enqueue would have been fired by the FOLLOWING workflow's claim and the last one by
	whatever committed after the tick, which under a test's rollback is nothing at all. Committing after the
	registration instead makes the pair atomic: the row lock `_claim` took is held across both, so a cohort
	can never be left `Draining` with no job, and no job can exist for a claim that rolled back.
	"""
	if respect_switch and not _armed():
		return 0
	_reap_stranded()
	started = 0
	for name in _due_workflows():
		if not _claim(name):
			continue
		frappe.enqueue(
			_RUN_COHORT,
			queue=wakeups.WAKE_QUEUE,
			job_id=_booking_key(name),
			deduplicate=True,
			enqueue_after_commit=True,
			now=bool(frappe.flags.get("in_test")),
			workflow_name=name,
		)
		frappe.db.commit()
		started += 1
	return started


def _armed():
	"""The cohort's own switch, fresh. Its `requires` names the engine switch, so `is_enabled` answers both."""
	return automation.is_enabled(SWITCH_COHORT)


def _due_workflows():
	"""The one indexed question Part A materialised the columns for: mode plus a clock.

	A cohort mid-walk is excluded by the CLAIM, not by a second filter here: `_claim` requires IDLE and a
	walking cohort holds `Draining` from its first chunk to its last.
	"""
	from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import ARMED_STATE
	from tatva_connect.workflow_engine.registry import MODE_SCHEDULE

	return frappe.get_all(  # authz-ok: tier-a — workflow engine, scheduler context
		_WORKFLOW_DT,
		filters={
			"lifecycle_state": ARMED_STATE,
			"trigger_mode": MODE_SCHEDULE,
			"trigger_next_run_at": ["<=", now_datetime()],
		},
		pluck="name",
		order_by="trigger_next_run_at asc",
		limit=thresholds.MAX_DUE_PER_SWEEP,
	)


def _claim(workflow_name):
	"""Take this cohort, or lose to whoever already has it. Exactly one caller can win.

	The engine's own claim, three lines from here: `get_value(..., for_update=True)` takes a row lock and
	filters on the state we require in the SAME read — `wakeups.drive_journey:170`, `signals:79` and
	`interpreter:375` all do this. A second sweep BLOCKS on the lock until this one commits, then
	re-evaluates its filter, sees `Draining` and returns nothing. That is the whole guarantee, and it is
	the reason the state has to be inside the filter rather than checked after the read.

	THE CLOCK IS NOT TOUCHED HERE — `_release` moves it, and only when the occurrence is really over. It
	used to be pushed forward as part of the claim, on the reasoning that a workflow still showing due
	would be picked up by the very next tick and drained twice. The state IS that guarantee: this read
	filters on `cohort_state` under the row lock, so a still-due row that is `Draining` loses the claim.
	The clock was belt over braces, and it cost the scheduled lane every cohort larger than one burst.

	`cohort_abort` is not cleared here either. The flag belongs to an OCCURRENCE, and an occurrence ends
	where the clock moves; cleared at claim time, a resume after a pace-out would wipe an abort the
	operator raised in the gap and walk on.

	IT DOES NOT COMMIT — `sweep` does, once, after it has also registered the enqueue. The lock is held
	until that commit, so the guarantee above is unchanged; what changes is that the claim and the job it
	exists to start can no longer land apart.
	"""
	if not frappe.db.get_value(
		_WORKFLOW_DT, {"name": workflow_name, "cohort_state": ["in", ["", None]]}, "name", for_update=True,
	):
		return False
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, {
		"cohort_state": DRAINING,
		# The heartbeat starts at the claim, or a cohort whose first selector scan is slow reads as dead.
		"cohort_progress_at": now_datetime(),
	}, update_modified=False)
	return True


def _reap_stranded():
	"""Hand back a claim whose walker died — W4.2's promised reaper, and `_release`'s second caller.

	`_release`'s only other caller is the `run_cohort` job itself, so an OOM, a deploy restart or an RQ
	timeout leaves the row `Draining` forever and every later `_claim` loses. There is no way back without
	a manual database write, which is not a recovery story.

	IT JUDGES BY THE BATCH THAT NEVER ARRIVED, not by silence. A cohort between chunks is deliberately still
	for `cohort_interval_seconds`, which the operator may set as high as an hour — judged on quiet alone, a
	healthy slow-paced cohort would be torn down mid-walk on every sweep. `cohort_next_chunk_at` says when
	the walk is next accounted for, so overdue by `DRAIN_DEAD_AFTER_MINUTES` is a lost booking and nothing
	else. `_stranded` also asks the older question for a claim that never got as far as booking.

	IT RESUMES RATHER THAN RESCHEDULING when the cursor says leads remain. Rescheduling was right when a
	killed job meant a dead occurrence; now the job is one short chunk and the cohort behind it is
	unfinished, so dating it tomorrow would silently drop every lead the walk had not reached. Releasing
	without the clock leaves the row due, and the next sweep resumes it from the cursor.

	FRAPPE NATIVE, and the APIs rejected are named. Not a second `scheduler_events` entry: this is the
	same question the sweep already asks of the same table on the same tick, and a separate cron would be
	a second thing to arm, time and switch off. Not `update_modified`/`modified` as the heartbeat either —
	the cursor write is PER LEAD, so a 10k cohort would dirty the row 10k times, collide with the
	lifecycle save that `apply_transition` makes (the Suspend that W10 turned into the kill), stamp
	`modified_by` as the drain for ever, and invalidate the document cache on every lead.
	"""
	stranded = _stranded()
	for name in stranded:
		unfinished = bool(frappe.db.get_value(_WORKFLOW_DT, name, "cohort_cursor"))
		# Closed with a REASON, never silently — `_release` commits this row along with the hand-back.
		frappe.log_error(
			title="cohort drain: a stranded claim was handed back",
			message=f"workflow={name} resumed={unfinished}",
		)
		_release(name, reschedule=not unfinished)
	return len(stranded)


def _stranded():
	"""Claims whose walker is gone: a booked chunk that never arrived, or a claim that never booked one."""
	dead_before = add_to_date(now_datetime(), minutes=-thresholds.DRAIN_DEAD_AFTER_MINUTES)
	overdue = frappe.get_all(  # authz-ok: tier-a — workflow engine, scheduler context
		_WORKFLOW_DT,
		filters={"cohort_state": DRAINING, "cohort_next_chunk_at": ["<", dead_before]},
		pluck="name",
		limit=thresholds.MAX_DUE_PER_SWEEP,
	)
	never_booked = frappe.get_all(  # authz-ok: tier-a — workflow engine, scheduler context
		_WORKFLOW_DT,
		filters={"cohort_state": DRAINING, "cohort_next_chunk_at": ["is", "not set"]},
		or_filters=[["cohort_progress_at", "<", dead_before], ["cohort_progress_at", "is", "not set"]],
		pluck="name",
		limit=thresholds.MAX_DUE_PER_SWEEP,
	)
	return list(dict.fromkeys(overdue + never_booked))


def abort(workflow_name):
	"""Stop THIS cohort at the next chunk boundary. Already-started runs are left alone.

	The product owner's decision, and the line matters: this is not a per-lead cancel — a journey that has
	begun keeps going, because killing one lead's journey mid-flight is a different question with
	different consequences, and it belongs with W10.
	"""
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, "cohort_abort", 1, update_modified=False)
	frappe.db.commit()


def run_cohort(workflow_name, chunk=None, respect_switch=True, book_next=True):
	"""Start ONE chunk of this cohort's journeys, book the next chunk, and return. The queued entry point.

	THE DRAIN PACES ITSELF. One chunk per job, and the job books its own successor `_pace` seconds out, so
	the rate is this engine's own and no outside clock contributes to it. `sweep` is what NOTICES a booking
	that never arrived — a backstop, never the motor. Each job therefore lives for seconds rather than
	minutes, which is what keeps the lane free for wakes, sends and a Suspend between chunks.

	It used to loop until a token bucket ran dry and then stop dead, leaving the 15-minute sweep to bring
	it back: the delivered rate was one chunk per SWEEP, not per minute, and no constant said so.

	RESPECTING THE SWITCH IS THE DEFAULT, and that is the whole safety property: the sweep enqueues this
	by name and kwargs, so anything it does not pass is whatever the signature says. It said False, so the
	only caller in production read no switch at all. A caller that genuinely wants to bypass the switch —
	a test driving the walk itself — says so out loud.

	The switch and the abort flag both end the OCCURRENCE, exactly as they did when this was one long loop:
	they release WITH the clock, so the cohort comes back at its next scheduled time and not before.
	"""
	if respect_switch and not _armed():
		_release(workflow_name)
		return 0
	version = _version_of(workflow_name)
	if not version:
		_release(workflow_name)
		return 0
	row = frappe.db.get_value(
		_WORKFLOW_DT, workflow_name, ["cohort_cursor", "cohort_abort"], as_dict=True,
	) or frappe._dict()
	if row.get("cohort_abort"):
		_release(workflow_name)
		return 0

	size, interval = _pace(workflow_name)
	config = _trigger_config(workflow_name)
	subject = config.get("subject_doctype") or "CRM Lead"

	# `scanned_to` is how far the selector READ, which is not the last lead it MATCHED. The cursor follows the scan, so leads the criteria reject are passed once and never walked again.
	leads, scanned_to = cohort.matching_leads(
		subject, config, after=row.get("cohort_cursor"), limit=chunk or size,
	)
	if not leads:
		_release(workflow_name, clear_cursor=True)
		return 0

	started = 0
	for lead in leads:
		started += 1 if _start_one(workflow_name, version, lead) else 0
		# The cursor stops AT the last lead actually started, never at the scan frontier — a worker that dies mid-chunk must not carry it past someone who was never begun.
		frappe.db.set_value(_WORKFLOW_DT, workflow_name,
		                    {"cohort_cursor": lead, "cohort_progress_at": now_datetime()},
		                    update_modified=False)
	frappe.db.set_value(_WORKFLOW_DT, workflow_name,
	                    {"cohort_cursor": scanned_to, "cohort_progress_at": now_datetime()},
	                    update_modified=False)
	frappe.db.commit()

	# Leads remain, so the CLAIM IS KEPT and only the booking carries the walk on. See `_pause`.
	if book_next:
		_pause(workflow_name, interval)
	return started


def _start_one(workflow_name, version, lead):
	"""One ordinary journey for one lead, through the entry a save uses. True iff one was really started.

	Called INLINE, never enqueued: N enqueues is the shape this whole chunk exists to avoid. A lead that
	is already running is not an error — `_start_one`'s `active_key` UNIQUE index makes the second attempt
	a no-op, which is the second line of defence behind the cursor.

	W8.4 gave `start_journey` a REFUSAL to return, and the count has to respect it: a cohort of leads who
	have all already completed the workflow would otherwise report every one of them as started, which is
	the receipt saying the opposite of what happened. The cursor still advances past a refused lead — the
	answer will not change on the next tick.
	"""
	from tatva_connect.workflow_engine import triggers

	try:
		return not triggers.start_journey(workflow_name, version, lead)
	except Exception:
		# One lead's failure is not the cohort's. It is recorded and the walk goes on, exactly as a
		# per-record failure does in `partner_bulk_worker._drain`.
		frappe.db.rollback()
		frappe.log_error(title="cohort drain: a lead failed to start",
		                 message=f"workflow={workflow_name} lead={lead}\n{frappe.get_traceback()}")
		return False


def _release(workflow_name, clear_cursor=False, reschedule=True):
	"""Hand the cohort back. A finished walk clears its cursor; a paused one keeps it to resume from.

	THE CLOCK MOVES HERE, NOT AT THE CLAIM, and that is what makes the scheduled lane work above one
	chunk. A walk with leads still in front of it has not finished its occurrence, so it releases with
	`reschedule=False` and the row stays due. Advanced at claim time instead, the row was already dated
	tomorrow the moment the walk began — so a pause fell out of `_due_workflows` and the cohort gained one
	chunk per occurrence and no more. A Daily cohort of 10,000 took months, silently, with the cursor and
	the counts all reading correct.

	`cohort_abort` is cleared on the same condition and for the same reason: the flag belongs to an
	OCCURRENCE, and the occurrence is what the clock ends. A resume mid-pause must keep it, or an abort
	raised while the cohort sat idle-but-due would be wiped by the very next claim.

	RELEASING IS FOR AN OCCURRENCE THAT IS OVER, never for a walk that is merely between chunks — that is
	`_pause`. Any booking still outstanding is dropped here, or a released cohort would be walked on a
	minute later by an alarm nobody expects.
	"""
	values = {"cohort_state": IDLE, "cohort_next_chunk_at": None}
	if clear_cursor:
		values["cohort_cursor"] = ""
	if reschedule:
		values["cohort_abort"] = 0
		values["trigger_next_run_at"] = cohort.next_run_at(_trigger_config(workflow_name))
	wakeups.forget_on_lane(_booking_key(workflow_name))
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, values, update_modified=False)
	frappe.db.commit()


def _pause(workflow_name, seconds):
	"""Between chunks: KEEP THE CLAIM, record when the next chunk is owed, and book it.

	THE CLAIM IS HELD FOR THE WHOLE COHORT, and it is not a lock — it is one word in one column. No worker,
	no connection and no row lock survives this call; the job exits and the lane is free. What the flag buys
	is that nothing else can take this cohort mid-walk: `_claim` filters on IDLE, so the sweep cannot start a
	second walker, and `cohort_state` stays `Draining` so the operator's "Stop cohort" button stays on screen
	for as long as the cohort is really running. Releasing between chunks took both of those away.

	`cohort_next_chunk_at` is the DURABLE half of the booking — the job in Redis is a copy and §6.2 is that a
	copy is never a fact. It is what `_stranded` judges by, so a cohort waiting out a long interval reads as
	healthy rather than as a dead worker.
	"""
	due = add_to_date(now_datetime(), seconds=seconds)
	wakeups.schedule_on_lane(due, _RUN_COHORT, {"workflow_name": workflow_name}, _booking_key(workflow_name))
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, {"cohort_next_chunk_at": due}, update_modified=False)
	frappe.db.commit()


def _pace(workflow_name):
	"""`(leads per chunk, seconds until the next one)` — the operator's row, with this workflow's own interval winning.

	A workflow that DIALS is paced by the voice provider's ceiling and one that messages by WhatsApp's, so
	the interval is overridable per workflow while the chunk stays global; `thresholds` holds the defaults
	for a site that has never opened the settings.
	"""
	size, interval = frappe.get_cached_doc(_PACE_DT).pace()
	return size, frappe.db.get_value(_WORKFLOW_DT, workflow_name, "cohort_interval_seconds") or interval


def _trigger_config(workflow_name):
	"""This workflow's Trigger config, through the ONE reader."""
	from tatva_connect.workflow_engine import registry

	node = frappe.get_all(
		"CRM Workflow Node",
		filters={"workflow": workflow_name, "node_type": registry.TRIGGER},
		fields=["config_json"],
		limit=1,
	)
	return registry.config_of(node[0]) if node else {}


def _version_of(workflow_name):
	from tatva_connect.workflow_engine import versions

	return versions.current_name(workflow_name)
