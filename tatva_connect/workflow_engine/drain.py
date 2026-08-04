# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W7.2 PART B — the drain that walks a cohort. ONE job, a keyset cursor, committed per chunk.

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
  * the pacing       — `api._base._bucket_pair`, the partner API's own atomic Redis bucket
  * the drain shape  — `api.partner_bulk_worker`: claim the row, chunk, commit each, re-read the abort
                       flag between chunks, resume from stored state. Same moves, same order.

DORMANT. `Workflow::Cohort::drain` ships OFF, and both the sweep and the job re-read it — the job because
the switch may be turned off between the two, which is exactly what `triggers.start_journey` guards against.
"""
import frappe
from frappe.utils import now_datetime

from tatva_connect.automation import settings as automation
from tatva_connect.workflow_engine import cohort, thresholds

SWITCH_COHORT = "Workflow::Cohort::drain"

_WORKFLOW_DT = "CRM Workflow"

# W4.3 — every number this drain paces itself by is DECLARED in `thresholds`, never re-stated here.

IDLE, DRAINING = "", "Draining"


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
			"tatva_connect.workflow_engine.drain.run_cohort",
			queue="workflow",
			job_id=f"cohort-drain::{name}",
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
	"""The one indexed question Part A materialised the columns for: mode plus a clock."""
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

	IT READS THE HEARTBEAT `run_cohort` ALREADY WRITES. `cohort_progress_at` is stamped by the claim and
	moved again by every cursor write, so "no progress for `DRAIN_DEAD_AFTER_MINUTES`" means a dead worker
	and nothing else: a walk that paced out released before it stopped, and so did one the switch stopped.
	A row still `Draining` with NO stamp at all was claimed before this column existed and is stranded by
	definition, which is why the age query and the unstamped case are asked together.

	The stamp rides the writes `run_cohort` was already making — the claim, and each cursor write — so a
	walk that is moving says so at no extra query, and one that stops saying so is a worker to free.

	It reschedules, because a dead occurrence is over. The cursor is left alone, so the next claim resumes
	from where the dead worker reached instead of re-walking the cohort from the top.

	FRAPPE NATIVE, and the APIs rejected are named. Not a second `scheduler_events` entry: this is the
	same question the sweep already asks of the same table on the same tick, and a separate cron would be
	a second thing to arm, time and switch off. Not `update_modified`/`modified` as the heartbeat either —
	the cursor write is PER LEAD, so a 10k cohort would dirty the row 10k times, collide with the
	lifecycle save that `apply_transition` makes (the Suspend that W10 turned into the kill), stamp
	`modified_by` as the drain for ever, and invalidate the document cache on every lead.
	"""
	dead_before = frappe.utils.add_to_date(now_datetime(), minutes=-thresholds.DRAIN_DEAD_AFTER_MINUTES)
	stranded = frappe.get_all(  # authz-ok: tier-a — workflow engine, scheduler context
		_WORKFLOW_DT,
		filters={"cohort_state": DRAINING},
		or_filters=[["cohort_progress_at", "<", dead_before], ["cohort_progress_at", "is", "not set"]],
		pluck="name",
		limit=thresholds.MAX_DUE_PER_SWEEP,
	)
	for name in stranded:
		# Closed with a REASON, never silently — `_release` commits this row along with the hand-back.
		frappe.log_error(
			title="cohort drain: a stranded claim was handed back",
			message=f"workflow={name} no progress since before {dead_before}",
		)
		_release(name)
	return len(stranded)


def abort(workflow_name):
	"""Stop THIS cohort at the next chunk boundary. Already-started runs are left alone.

	The product owner's decision, and the line matters: this is not a per-lead cancel — a journey that has
	begun keeps going, because killing one lead's journey mid-flight is a different question with
	different consequences, and it belongs with W10.
	"""
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, "cohort_abort", 1, update_modified=False)
	frappe.db.commit()


def run_cohort(workflow_name, chunk=None, stop_after_chunks=None, respect_switch=True):
	"""Walk this workflow's cohort, starting one ordinary journey per lead. The queued entry point.

	Re-reads the switch and the abort flag AT EVERY CHUNK BOUNDARY and commits each, so a cohort can be
	stopped mid-flight and a killed worker resumes from the stored cursor rather than from the top.

	RESPECTING THE SWITCH IS THE DEFAULT, and that is the whole safety property: the sweep enqueues this
	by name and kwargs, so anything it does not pass is whatever the signature says. It said False, so the
	only caller in production read no switch at all. A caller that genuinely wants to bypass the switch —
	a test driving the walk itself — says so out loud.
	"""
	chunk = chunk or thresholds.DRAIN_CHUNK
	config = _trigger_config(workflow_name)
	subject = config.get("subject_doctype") or "CRM Lead"
	version = _version_of(workflow_name)
	if not version:
		_release(workflow_name)
		return 0

	# Declared out here because the switch and abort breaks never reach the per-chunk reset below.
	started, chunks, paced_out = 0, 0, False
	while True:
		# BOTH STOPS ARE READ HERE, on every pass, and both leave by the same door: the `break` falls to
		# `_release` below, because a cohort left `Draining` is one `_claim` can never match again.
		if respect_switch and not _armed():
			break
		row = frappe.db.get_value(
			_WORKFLOW_DT, workflow_name, ["cohort_cursor", "cohort_abort"], as_dict=True,
		) or frappe._dict()
		if row.get("cohort_abort"):
			break
		# `scanned_to` is how far the selector READ, which is not the last lead it MATCHED. The cursor
		# follows the scan, so leads the criteria reject are passed once and never walked again.
		leads, scanned_to = cohort.matching_leads(
			subject, config, after=row.get("cohort_cursor"), limit=chunk,
		)
		if not leads:
			_release(workflow_name, clear_cursor=True)
			return started

		paced_out = False
		for lead in leads:
			if not _take_token(workflow_name):
				# The provider is full. PAUSE — never skip: a lead that was not started must stay in front
				# of the cursor so the next tick picks it up, or the cohort silently loses people.
				paced_out = True
				break
			started += 1 if _start_one(workflow_name, version, lead) else 0
			# The cursor stops AT the last lead actually started, never at the scan frontier — a pause must
			# not carry the cursor past someone who was never begun.
			frappe.db.set_value(_WORKFLOW_DT, workflow_name,
			                    {"cohort_cursor": lead, "cohort_progress_at": now_datetime()},
			                    update_modified=False)
		if not paced_out:
			frappe.db.set_value(_WORKFLOW_DT, workflow_name,
			                    {"cohort_cursor": scanned_to, "cohort_progress_at": now_datetime()},
			                    update_modified=False)
		frappe.db.commit()

		chunks += 1
		if paced_out:
			break
		if stop_after_chunks and chunks >= stop_after_chunks:
			return started
	# A pace-out is not a finished occurrence, so it releases without moving the clock. See `_release`.
	_release(workflow_name, reschedule=not paced_out)
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
	burst. A walk that stopped for PACING has not finished its occurrence, so it releases with
	`reschedule=False`: the row stays due, the next `sweep` tick claims it again, and the cursor carries it
	on. Advanced at claim time instead, the row was already dated tomorrow the moment the walk began — so a
	pace-out fell out of `_due_workflows` and the cohort gained one burst per occurrence and no more. A
	Daily cohort of 10,000 took months, silently, with the cursor and the counts all reading correct.

	`cohort_abort` is cleared on the same condition and for the same reason: the flag belongs to an
	OCCURRENCE, and the occurrence is what the clock ends. A resume mid-pause must keep it, or an abort
	raised while the cohort sat idle-but-due would be wiped by the very next claim.
	"""
	values = {"cohort_state": IDLE}
	if clear_cursor:
		values["cohort_cursor"] = ""
	if reschedule:
		values["cohort_abort"] = 0
		values["trigger_next_run_at"] = cohort.next_run_at(_trigger_config(workflow_name))
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, values, update_modified=False)
	frappe.db.commit()


def _take_token(workflow_name):
	"""Charge one journey against the partner API's OWN atomic bucket — global and per-workflow, both must pay.

	`_bucket_pair` is the one limiter in this app and its Lua is what makes the check atomic under
	concurrency. It is imported rather than copied: a second limiter would be a second answer to "may this
	go out now", and the two would disagree the first time either was tuned. Fail-open is its own contract
	— a Redis outage must not stop a cohort.
	"""
	from tatva_connect.api._base import _bucket_pair

	verdict = _bucket_pair(
		True, 1,
		"cohort", thresholds.DRAIN_RATE, thresholds.DRAIN_BURST,
		f"cohort:{workflow_name}", thresholds.DRAIN_RATE, thresholds.DRAIN_BURST, thresholds.DRAIN_WINDOW,
	)
	if not verdict:
		return True  # exempt or unreadable — the limiter's own fail-open
	retry_after, _remaining, _shared = verdict
	return retry_after is None


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
