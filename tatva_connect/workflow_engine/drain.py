# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W7.2 PART B — the drain that walks a cohort. ONE job, a keyset cursor, committed per chunk.

A COHORT IS A RUN FACTORY, NOT A SECOND ENGINE. The schedule fires, this walks the leads the Trigger's
criteria select, and each one gets its OWN ordinary run through `triggers.start_run` — the SAME entry the
record-event lane calls. Nothing about node contracts, park/resume or the interpreter changes, and there
is no recipient-set inside a run: that is the evals model and it is deliberately not ours.

WHY ONE JOB AND NOT ONE PER LEAD. `frappe.enqueue` THROWS above `MAX_QUEUED_JOBS = 500`. A 2,000-lead
cohort turned into 2,000 jobs errors partway and leaves the rest of the cohort silently unstarted — the
worst shape available, because it looks like it worked. So the sweep enqueues ONE drain per due workflow
and the drain does the walking.

WHY KEYSET AND NOT OFFSET. `OFFSET 10000` re-reads ten thousand rows to skip them, and rows shift between
pages while the table is written to underneath — so a lead is started twice or never. The cursor is the
lead name, ascending, stored as it goes, and a resume asks for `name > cursor`.

WHAT IT REUSES, SO THERE IS NO SECOND BRAIN:
  * the criteria     — `cohort.matching_leads`, the same grain + `rules.predicate_match` the preview counts with
  * the start        — `triggers.start_run`, the same entry a save uses; its `active_key` UNIQUE index is
                       what makes a resume unable to double-start a lead even if the cursor were lost
  * the pacing       — `api._base._bucket_pair`, the partner API's own atomic Redis bucket
  * the drain shape  — `api.partner_bulk_worker`: claim the row, chunk, commit each, re-read the abort
                       flag between chunks, resume from stored state. Same moves, same order.

DORMANT. `Workflow::Cohort::drain` ships OFF, and both the sweep and the job re-read it — the job because
the switch may be turned off between the two, which is exactly what `triggers.start_run` guards against.
"""
import frappe
from frappe.utils import now_datetime

from tatva_connect.automation import settings as automation
from tatva_connect.workflow_engine import ENGINE_SWITCH, cohort, thresholds

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
	"""
	if respect_switch and not _armed():
		return 0
	started = 0
	for name in _due_workflows():
		if not _claim(name):
			continue
		frappe.enqueue(
			"tatva_connect.workflow_engine.drain.run_cohort",
			queue="workflow",
			job_id=f"cohort-drain::{name}",
			deduplicate=True,
			now=bool(frappe.flags.get("in_test")),
			workflow_name=name,
		)
		started += 1
	return started


def _armed():
	"""Both switches, fresh. The engine's own and the cohort's — a cohort is the engine at volume."""
	return automation.is_enabled(ENGINE_SWITCH) and automation.is_enabled(SWITCH_COHORT)


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
	filters on the state we require in the SAME read — `wakeups.drive_instance:170`, `signals:79` and
	`interpreter:375` all do this. A second sweep BLOCKS on the lock until this one commits, then
	re-evaluates its filter, sees `Draining` and returns nothing. That is the whole guarantee, and it is
	the reason the state has to be inside the filter rather than checked after the read.

	The clock is pushed forward as part of the claim: a workflow still showing due after being claimed
	would be picked up again by the very next tick and drained twice.
	"""
	if not frappe.db.get_value(
		_WORKFLOW_DT, {"name": workflow_name, "cohort_state": ["in", ["", None]]}, "name", for_update=True,
	):
		return False
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, {
		"cohort_state": DRAINING,
		"cohort_abort": 0,
		"trigger_next_run_at": cohort.next_run_at(_trigger_config(workflow_name)),
	}, update_modified=False)
	frappe.db.commit()
	return True


def abort(workflow_name):
	"""Stop THIS cohort at the next chunk boundary. Already-started runs are left alone.

	The product owner's decision, and the line matters: this is not a per-lead cancel — a run that has
	begun keeps going, because killing one lead's journey mid-flight is a different question with
	different consequences, and it belongs with W10.
	"""
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, "cohort_abort", 1, update_modified=False)
	frappe.db.commit()


def run_cohort(workflow_name, chunk=None, stop_after_chunks=None, respect_switch=True):
	"""Walk this workflow's cohort, starting one ordinary run per lead. The queued entry point.

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

	started, chunks = 0, 0
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
			_start_one(workflow_name, version, lead)
			started += 1
			# The cursor stops AT the last lead actually started, never at the scan frontier — a pause must
			# not carry the cursor past someone who was never begun.
			frappe.db.set_value(_WORKFLOW_DT, workflow_name, "cohort_cursor", lead,
			                    update_modified=False)
		if not paced_out:
			frappe.db.set_value(_WORKFLOW_DT, workflow_name, "cohort_cursor", scanned_to,
			                    update_modified=False)
		frappe.db.commit()

		chunks += 1
		if paced_out:
			break
		if stop_after_chunks and chunks >= stop_after_chunks:
			return started
	_release(workflow_name)
	return started


def _start_one(workflow_name, version, lead):
	"""One ordinary run for one lead, through the entry a save uses.

	Called INLINE, never enqueued: N enqueues is the shape this whole chunk exists to avoid. A lead that
	is already running is not an error — `_start_one`'s `active_key` UNIQUE index makes the second attempt
	a no-op, which is the second line of defence behind the cursor.
	"""
	from tatva_connect.workflow_engine import triggers

	try:
		triggers.start_run(workflow_name, version, lead)
	except Exception:
		# One lead's failure is not the cohort's. It is recorded and the walk goes on, exactly as a
		# per-record failure does in `partner_bulk_worker._drain`.
		frappe.db.rollback()
		frappe.log_error(title="cohort drain: a lead failed to start",
		                 message=f"workflow={workflow_name} lead={lead}\n{frappe.get_traceback()}")


def _release(workflow_name, clear_cursor=False):
	"""Hand the cohort back. A finished walk clears its cursor; a paused one keeps it to resume from."""
	values = {"cohort_state": IDLE}
	if clear_cursor:
		values["cohort_cursor"] = ""
	frappe.db.set_value(_WORKFLOW_DT, workflow_name, values, update_modified=False)
	frappe.db.commit()


def _take_token(workflow_name):
	"""Charge one run against the partner API's OWN atomic bucket — global and per-workflow, both must pay.

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
