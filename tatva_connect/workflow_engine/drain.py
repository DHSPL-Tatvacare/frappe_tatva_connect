# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The workflow drain: one paced pass over the engine's durable rows, shaped like `webhooks/spine.py`.

A pass wakes parked journeys past `resume_at` (`wakeups.wake_due`), walks scheduled workflows past
`trigger_next_run_at` (`cohort.start_due`) and re-drives buffered signals (`signals.redrive`), oldest first and at
the site's pace. One pass runs at a time under the drain lock (`utils.book_drain`, shared with the spine). Each pass
stops at its time slice and books the next: one pace interval out while work is due, otherwise the earliest deadline.
Parks and activations pull that booking forward; the */15 backstop (`wakeups.sweep`) books a pass if one was lost.
"""
import time

import frappe

from tatva_connect import automation
from tatva_connect.utils import book_drain, hold_drain, lane_has_room, release_drain
from tatva_connect.workflow_engine import ENGINE_SWITCH, thresholds, wakeups

# The drain lock, the queued pass and the booked passes are all keyed by this.
DRAIN_KEY = "workflow-drain"
_PASS = "tatva_connect.workflow_engine.drain.run"


def kick():
	"""Queue a pass now. Deduplicated on the lane, and the pass takes the drain lock itself, so a queued pass never races a running one."""
	return frappe.enqueue(
		_PASS, queue=wakeups.WAKE_QUEUE, job_id=DRAIN_KEY, deduplicate=True, enqueue_after_commit=True,
		now=bool(frappe.flags.get("in_test")),
	)


def pull_forward(when):
	"""Book a pass no later than `when`, never pushing an earlier booking back. A same-instant race is bounded by the backstop."""
	wakeups.schedule_on_lane(when, _PASS, {}, DRAIN_KEY, only_if_earlier=True)


def has_work():
	"""True when a due journey, a due cohort or a signal a parked journey can take is waiting — the backstop's one question."""
	from tatva_connect.workflow_engine import cohort, signals

	return bool(wakeups.due_journeys(limit=1) or cohort.due_workflows(limit=1) or signals.claimable(limit=1))


def run():
	"""One pass: wake due journeys, walk due cohorts, re-drive buffered signals, then book the next pass. Returns rows moved.

	The lock is held for no longer than the lane lets a job live, so it cannot lapse under a running pass. A pass that
	finds it taken books a retry one pace interval out; the holder books its own successor after releasing it.
	"""
	from tatva_connect.tatva_connect.doctype.crm_cohort_pace_settings import crm_cohort_pace_settings as pace
	from tatva_connect.workflow_engine import cohort, signals

	if not automation.is_enabled(ENGINE_SWITCH):
		return 0  # a disarmed engine moves nothing; the backstop queues a pass once it is armed
	if frappe.session.user == "Guest":
		frappe.set_user("Administrator")
	size, interval = pace.site_pace()
	started = frappe.utils.now_datetime()
	lease = interval * thresholds.DRAIN_LEASE_MULTIPLE
	if not book_drain(DRAIN_KEY, lease):
		pull_forward(frappe.utils.add_to_date(started, seconds=interval))
		frappe.db.commit()
		return 0
	moved = 0
	# The lease is short so a dead pass frees the pile fast; a live one says it is still here as it works.
	def renew():
		hold_drain(DRAIN_KEY, lease)

	try:
		until = time.monotonic() + interval  # a pass never outlives its own period
		# A lane already deep in send jobs is left to empty: this pass adds nothing to it.
		if lane_has_room(wakeups.WAKE_QUEUE):
			moved += wakeups.wake_due(size, until, renew)
			moved += cohort.start_due(size - moved, until, renew)
			moved += signals.redrive(size - moved, until, renew)
	finally:
		release_drain(DRAIN_KEY)
	_book_next(interval, started)
	frappe.db.commit()
	return moved


def _book_next(interval, started):
	"""Work still due: one pace interval from when this pass BEGAN, so the interval is a period and not a gap added to it. Otherwise the earliest deadline ahead; an unclaimable signal is left to the backstop."""
	from tatva_connect.workflow_engine import cohort

	now = frappe.utils.now_datetime()
	if wakeups.due_journeys(limit=1) or cohort.due_workflows(limit=1):
		wakeups.schedule_on_lane(max(frappe.utils.add_to_date(started, seconds=interval), now), _PASS, {}, DRAIN_KEY)
		return
	ahead = [at for at in (wakeups.next_resume_at(), cohort.next_due_at()) if at]
	if ahead:
		wakeups.schedule_on_lane(min(ahead), _PASS, {}, DRAIN_KEY)
