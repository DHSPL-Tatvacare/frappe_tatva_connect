"""Scheduled wakeups + the reliability backstop.

PRINCIPLE (F5): the durable Journey row is the source of truth; every enqueue is only a latency
optimisation. If a wake job is lost (Redis flush, worker death), the reconciler still drives the Journey
forward from its durable state - nothing depends on an RQ job surviving.

`sweep()` is the ONE scheduled entry (hooks.scheduler_events, ~*/15), gated on the SWEEP switch alone -
its `requires` names the engine switch, so `is_enabled` answers for both and nothing here checks the pair
by hand. An operator can still pause the sweep without killing the engine. It runs, in order:
  * `timer_sweep` - the TIMER side: `status='Parked' AND resume_at<=now`, claim each `for_update`,
    `advance`, commit PER ROW (a worker killed mid-sweep never replays a segment whose sends already left).
  * `reconciler_sweep` - the reliability backstop: re-drives (a) due-timer Parked rows and (b) a `Parked`
    Journey whose `awaiting_signal` already has a matching Pending inbox row but was never woken (a lost
    enqueue). Overlaps timer_sweep on (a) by design - a re-drive of an already-advanced row is a claimed
    no-op (F6) - so the reconciler is a COMPLETE standalone backstop.
  * `_purge_stale_signals` - THE reaper (W4.4): expire orphan Pending rows, delete old terminal ones.

Every drive claims the Journey `for_update` and re-checks status BEFORE work (F6), and sets
`frappe.flags.in_workflow` so the engine's own writes don't re-enter entry/signal detection.
"""
import frappe

from tatva_connect import automation
from tatva_connect.workflow_engine import ENGINE_SWITCH, SWEEP_SWITCH, interpreter, thresholds

JOURNEY_DT = interpreter.JOURNEY_DT
SIGNAL_DT = interpreter.SIGNAL_DT

# The lane Frappe's own scheduler does not manage, so our RQ scheduler can service it without two
# schedulers fighting. Registered in common_site_config `workers`, exactly as `partner_bulk` is.
WAKE_QUEUE = "workflow"

# THE DEPLOY CONTRACT FOR THIS LANE, in the repo rather than in one machine's compose file.
# Until now it existed ONLY in `.localdev/compose.yml`, which is git-excluded — so the engine ran here
# and nowhere else, and a deploy that missed it would write timer alarms into a queue nothing services.
# That failure is SILENT: every journey parks correctly, every alarm is set, and none of them ever fires.
# `assert_lane_registered` below is what makes it loud, and these are the three things it is about:
#
#   1. bench set-config -gp workers "{'workflow': {'background_workers': 1, 'timeout': 1500}}"
#   2. bench worker --queue workflow                                     (a process, one per lane)
#   3. bench --site <site> execute tatva_connect.workflow_engine.wakeups.run_wake_scheduler
#
# (3) is our RQ scheduler for this lane. Frappe hard-disables RQ's scheduler on both worker paths so its
# own is the only one running — correct, and it means a lane Frappe does not manage is an empty lane.
# It is NOT load-bearing: kill it and parked journeys still wake off the */15 sweep, late.
LANE_WORKER_COMMAND = f"bench worker --queue {WAKE_QUEUE}"
LANE_SCHEDULER_COMMAND = "bench --site <site> execute tatva_connect.workflow_engine.wakeups.run_wake_scheduler"


def assert_lane_registered():
	"""after_migrate: refuse a site that has ARMED the engine without registering its lane.

	Gated on the engine switch on purpose. The engine ships dormant, so a fresh install and every bench
	that never turned it on are correct with no lane at all — and a `throw` there would fail
	`install-app` itself, since `workers` cannot be set before the app that needs it exists. The moment
	an operator arms the engine, the lane stops being optional and the next migrate says so.
	"""
	assert_lane(
		WAKE_QUEUE, thresholds.WAKE_JOB_TIMEOUT, ENGINE_SWITCH,
		f"Register it, then run `{LANE_WORKER_COMMAND}` and `{LANE_SCHEDULER_COMMAND}`.",
	)


def assert_lane(queue, min_timeout, switch, remedy):
	"""The lane check itself, shared: an armed tier must have the worker lane it enqueues to."""
	if not automation.is_enabled(switch):
		return
	lane = (frappe.conf.get("workers") or {}).get(queue)
	if not lane:
		frappe.throw(
			f"This tier is armed but the `{queue}` lane is not registered in common_site_config "
			f"`workers`. Its jobs would be enqueued and silently never execute. {remedy}"
		)
	if (lane.get("timeout") or 0) < min_timeout:
		frappe.throw(
			f"The `{queue}` lane's timeout is {lane.get('timeout')}s, below the declared "
			f"{min_timeout}s a job on it may run. A long job would be killed mid-flight."
		)


def schedule_on_lane(due, method, kwargs, key):
	"""Book ONE job on this lane for a future moment. THE engine's only way to schedule delayed work.

	`frappe.enqueue` has no delay parameter (`background_jobs.py:76` — the signature carries `now` and
	`enqueue_after_commit`, and nothing else about time), and Frappe hard-disables RQ's scheduler on both
	worker paths (`background_jobs.py:359`, `:364-366`) so that its own scheduler is the only one running.
	That is a reason not to run two schedulers, not a reason to reject delayed work: this lane is one
	Frappe's scheduler does not manage, and `run_wake_scheduler` services it. `enqueue_at` takes the
	identical job `enqueue_call` already builds, so this adds nothing to the queue layer.

	AFTER COMMIT, always: a booking must not survive the transaction that asked for it rolling back. The
	booking already held under this key is dropped FIRST and unconditionally, so a re-book REPLACES rather
	than stacks — a second booking for the same subject would fire early, which is worse than late.

	EXTRACTED, so the parked journey and the cohort drain share one way to wait. A second hand-rolled
	`enqueue_at` would be a second answer to "how does this engine come back later", and the two would
	disagree the first time either was touched — the timezone conversion below is exactly the kind of
	detail a copy gets wrong invisibly.
	"""
	from frappe.utils.background_jobs import create_job_id, get_queue

	queue_args = {
		"site": frappe.local.site,
		"user": frappe.session.user,
		"method": method,
		"event": None,
		"job_name": key,
		"is_async": True,
		"kwargs": kwargs,
	}
	job_id = create_job_id(key)
	queue = get_queue(WAKE_QUEUE)
	at = _as_utc(frappe.utils.get_datetime(due))

	def book():
		_forget_wake(queue, job_id)
		queue.enqueue_at(
			at,
			"frappe.utils.background_jobs.execute_job",
			kwargs=queue_args,
			job_timeout=thresholds.WAKE_JOB_TIMEOUT,
			job_id=job_id,
		)

	frappe.db.after_commit.add(book)


def forget_on_lane(key):
	"""Drop the booking held under this key, after commit. The other half of `schedule_on_lane`."""
	from frappe.utils.background_jobs import create_job_id, get_queue

	job_id = create_job_id(key)
	queue = get_queue(WAKE_QUEUE)
	frappe.db.after_commit.add(lambda: _forget_wake(queue, job_id))


def schedule_wake(name, resume_at):
	"""Set the alarm for a parked journey. The diary row is already written; this only makes it PUNCTUAL.

	The booking itself is `schedule_on_lane`'s; what belongs to a JOURNEY is the ceiling below. The payload
	is the NAME — `drive_journey` re-reads the row and re-claims it, so a stale or duplicated job is a
	no-op and a lost one is caught by the sweep.

	ABOVE `SCHEDULE_TO_DRAIN_HANDOVER` NO ALARM IS SET, and returns False so the park can say so. An alarm
	is a COPY of `resume_at`, which is already the truth, held in Redis for the whole wait — §6.2's rule is
	that a workflow entry in Redis is a pointer or a copy and never a fact, and §5.4 declares the sweep the
	volume path. Below the ceiling the copy buys punctuality cheaply; above it the sweep is already doing
	the work, so the journey goes LATE, NEVER WRONG.

	The forget still happens when no alarm is set, and unconditionally: `drive_journey` claims on status
	alone and never re-reads the clock, so an alarm left over from an EARLIER park does not go stale — it
	wakes the journey early.
	"""
	key = f"workflow-wake::{name}"
	if alarms_pending() >= thresholds.SCHEDULE_TO_DRAIN_HANDOVER:
		forget_on_lane(key)
		return False
	schedule_on_lane(resume_at, "tatva_connect.workflow_engine.wakeups.drive_journey", {"name": name}, key)
	return True


def alarms_pending(queue=None):
	"""How many alarms this lane is already holding — RQ's own registry, asked, never modelled.

	A ZCARD on the scheduled set. Keeping our own counter or cache key would be a second brain about a
	number Redis already holds, and it would drift the moment an alarm fired, was forgotten or expired.
	"""
	from frappe.utils.background_jobs import get_queue
	from rq.registry import ScheduledJobRegistry

	return ScheduledJobRegistry(queue=queue or get_queue(WAKE_QUEUE)).count


def lane_depth(queue=None):
	"""How many jobs are waiting on this lane — RQ's own count, asked, never modelled."""
	from frappe.utils.background_jobs import get_queue

	return get_queue(queue or WAKE_QUEUE).count


def _as_utc(due):
	"""RQ schedules in UTC; `resume_at` is written in the site's timezone. Converted once, here, because a
	timezone bug in an alarm is invisible until a patient is messaged at the wrong hour."""
	from datetime import timezone
	from zoneinfo import ZoneInfo

	if due.tzinfo is None:
		due = due.replace(tzinfo=ZoneInfo(frappe.utils.get_system_timezone()))
	return due.astimezone(timezone.utc)


def run_wake_scheduler(interval=1):
	"""Service the alarms on the workflow lane. Runs as its own process, forever.

	Frappe starts its own scheduler inside `FrappeWorker` and passes `with_scheduler=False` to RQ so the
	two cannot both run. This lane is not one Frappe's scheduler manages, so an RQ scheduler here services
	due alarms without contending with it. The W4 spike found a standalone process far easier to observe
	than the one `Worker.work(with_scheduler=True)` forks, and the per-queue Redis lock (TTL =
	interval + 60) already guarantees only one is ever moving jobs.

	IT IS NOT LOAD-BEARING. Kill it and every journey still wakes, late, off the sweep — which is exactly what
	covers the ~61s window while a dead scheduler's lock expires.
	"""
	from frappe.utils.background_jobs import generate_qname, get_redis_conn
	from rq.scheduler import RQScheduler

	RQScheduler([generate_qname(WAKE_QUEUE)], connection=get_redis_conn(), interval=interval).work()


def _forget_wake(queue, job_id):
	"""Drop any alarm already set for this journey, so a re-park replaces rather than stacks.

	`in registry` is RQ's own ZSCORE. `in registry.get_job_ids()` pulled EVERY scheduled id across the wire
	to test one membership, on every park — 45.16 ms against 1.25 ms at 20,000 pending.
	"""
	from rq.registry import ScheduledJobRegistry

	registry = ScheduledJobRegistry(queue=queue)
	if job_id in registry:
		registry.remove(job_id, delete_job=True)


def sweep():
	"""The scheduled tick: timer wake, signal backstop, stale-signal purge. Gated on the sweep switch."""
	if not automation.is_enabled(SWEEP_SWITCH):
		return
	timer_sweep()
	reconciler_sweep()
	_purge_stale_signals()


def timer_sweep():
	"""Wake every `Parked` Journey whose clock deadline has arrived, oldest first, capped. Per-row commit."""
	if not automation.is_enabled(SWEEP_SWITCH):
		return
	for name in _due_parked():
		drive_journey(name)
		frappe.db.commit()


def reconciler_sweep():
	"""The reliability backstop (F5): re-drive (a) due-timer Parked rows and (b) Parked rows whose awaited
	signal is already buffered but was never woken (a lost enqueue). Per-row commit. Overlaps timer_sweep on
	(a) BY DESIGN - a re-drive of an already-advanced row is a claimed no-op (F6), never a double-run - so
	the reconciler is a COMPLETE standalone backstop, not dependent on timer_sweep having run first."""
	if not automation.is_enabled(SWEEP_SWITCH):
		return
	for name in _due_parked():
		drive_journey(name)
		frappe.db.commit()
	for row in frappe.get_all(
		JOURNEY_DT,
		filters={"status": "Parked", "awaiting_signal": ["is", "set"]},
		fields=["name", "subject_doctype", "subject_name", "awaiting_signal", "awaiting_correlation"],
		limit=thresholds.SWEEP_PAGE,
	):
		if _has_pending_signal(row):
			drive_journey(row.name)
			frappe.db.commit()


def _purge_stale_signals():
	"""W4.4 — THE reaper: age the dead out of the inbox, then delete what has been terminal long enough.

	A row nothing ever claimed used to sit `Pending` for a month and then be DELETED. It reached no
	terminal state, so the inbox could not say what became of it; and all month it could still be claimed
	by a park — a message going out on a weeks-old signal the moment sends are armed.

	So expiry is a STATE. `Expired` is terminal, and inert because `pending_signal_filters` asks for
	`PENDING` and nothing else. One reaper, not two: this function already owned this table's age policy.
	"""
	now = frappe.utils.now_datetime()
	dead_before = frappe.utils.add_to_date(now, days=-thresholds.SIGNAL_DEAD_AFTER_DAYS)
	# Stamps `modified`, which is the timestamp the retention purge below then counts from.
	frappe.db.set_value(  # authz-ok: tier-a — workflow engine, scheduler context
		SIGNAL_DT, {"status": interpreter.PENDING, "creation": ["<", dead_before]}, "status", interpreter.EXPIRED
	)
	purge_before = frappe.utils.add_to_date(now, days=-thresholds.SIGNAL_RETENTION_DAYS)
	frappe.db.delete(SIGNAL_DT, {"status": ["in", interpreter.TERMINAL_SIGNAL_STATES], "modified": ["<", purge_before]})
	frappe.db.commit()


def drive_journey(name):
	"""Claim the Journey `for_update`, re-check it is still `Parked`, and `advance` (F6). Sets
	`in_workflow` so the segment's own writes to the subject don't re-enter entry/signal detection. Plumbing
	failures are logged, never raised, so one bad row never aborts a sweep."""
	frappe.flags.in_workflow = True
	try:
		claimed = frappe.db.get_value(
			JOURNEY_DT, {"name": name, "status": "Parked"}, ["name", "workflow"], as_dict=True, for_update=True,
		)
		if not claimed:
			return  # already claimed/advanced by another driver, or no longer parked (idempotent)
		if _workflow_retired(claimed.workflow):
			return  # W10 — retired means nothing is in flight, from the instant the lifecycle commits
		interpreter.advance(frappe.get_doc(JOURNEY_DT, name))
	except Exception:
		frappe.log_error(title="workflow: drive failed", message=f"journey={name} :: {frappe.get_traceback()}")
	finally:
		frappe.flags.in_workflow = False


def _workflow_retired(workflow):
	"""W10 — has this journey's workflow stopped being AVAILABLE? Retiring IS killing, so nothing may run.

	Suspended and Archived, from `RETIRED_STATES`, so this door and the transition that kills cannot disagree.

	A SECOND READ, deliberately, and the join was rejected rather than overlooked. Folding the lifecycle
	into the claim above means `SELECT ... FOR UPDATE` across a join, which locks the CRM Workflow row too
	— every wake of every journey would then serialise on one header and contend with the operator's own
	suspend save. `FOR UPDATE OF` would avoid that and is not something frappe's query builder exposes.
	So it is one indexed primary-key read, taken only after a journey has already been claimed, next to a
	`get_doc` of the whole journey that costs more.
	"""
	from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import RETIRED_STATES

	return frappe.db.get_value("CRM Workflow", workflow, "lifecycle_state") in RETIRED_STATES


def _due_parked():
	"""Names of `Parked` Journeys whose clock deadline has arrived, oldest first, capped."""
	return frappe.get_all(
		JOURNEY_DT,
		filters={"status": "Parked", "resume_at": ["<=", frappe.utils.now_datetime()]},
		order_by="resume_at asc",
		limit=thresholds.SWEEP_PAGE,
		pluck="name",
	)


def _has_pending_signal(row):
	"""True iff a live inbox row matches this Journey's awaited (subject, signal, correlation).

	Asks through `pending_signal_filters`, the ONE description of "a row that would wake this park", so the
	backstop can never re-drive a journey on a row the drive itself would then decline to consume. It carried
	its own copy of that dict until W4.4, which is what would have let an EXPIRED row wake a journey for ever.
	"""
	filters = interpreter.pending_signal_filters(
		row.subject_doctype, row.subject_name, row.awaiting_signal, row.awaiting_correlation
	)
	return bool(frappe.db.get_value(SIGNAL_DT, filters, "name"))
