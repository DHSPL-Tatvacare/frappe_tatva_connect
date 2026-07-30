"""Scheduled wakeups + the reliability backstop.

PRINCIPLE (F5): the durable Instance row is the source of truth; every enqueue is only a latency
optimisation. If a wake job is lost (Redis flush, worker death), the reconciler still drives the Instance
forward from its durable state - nothing depends on an RQ job surviving.

`sweep()` is the ONE scheduled entry (hooks.scheduler_events, ~*/15), gated on the SWEEP switch alone -
its `requires` names the engine switch, so `is_enabled` answers for both and nothing here checks the pair
by hand. An operator can still pause the sweep without killing the engine. It runs, in order:
  * `timer_sweep` - the TIMER side: `status='Parked' AND resume_at<=now`, claim each `for_update`,
    `advance`, commit PER ROW (a worker killed mid-sweep never replays a segment whose sends already left).
  * `reconciler_sweep` - the reliability backstop: re-drives (a) due-timer Parked rows and (b) a `Parked`
    Instance whose `awaiting_signal` already has a matching Pending inbox row but was never woken (a lost
    enqueue). Overlaps timer_sweep on (a) by design - a re-drive of an already-advanced row is a claimed
    no-op (F6) - so the reconciler is a COMPLETE standalone backstop.
  * `_purge_stale_signals` - THE reaper (W4.4): expire orphan Pending rows, delete old terminal ones.

Every drive claims the Instance `for_update` and re-checks status BEFORE work (F6), and sets
`frappe.flags.in_workflow` so the engine's own writes don't re-enter entry/signal detection.
"""
import frappe

from tatva_connect import automation
from tatva_connect.workflow_engine import ENGINE_SWITCH, SWEEP_SWITCH, interpreter, thresholds

INSTANCE_DT = interpreter.INSTANCE_DT
SIGNAL_DT = interpreter.SIGNAL_DT

# The lane Frappe's own scheduler does not manage, so our RQ scheduler can service it without two
# schedulers fighting. Registered in common_site_config `workers`, exactly as `partner_bulk` is.
WAKE_QUEUE = "workflow"

# THE DEPLOY CONTRACT FOR THIS LANE, in the repo rather than in one machine's compose file.
# Until now it existed ONLY in `.localdev/compose.yml`, which is git-excluded — so the engine ran here
# and nowhere else, and a deploy that missed it would write timer alarms into a queue nothing services.
# That failure is SILENT: every run parks correctly, every alarm is set, and none of them ever fires.
# `assert_lane_registered` below is what makes it loud, and these are the three things it is about:
#
#   1. bench set-config -gp workers "{'workflow': {'background_workers': 1, 'timeout': 1500}}"
#   2. bench worker --queue workflow                                     (a process, one per lane)
#   3. bench --site <site> execute tatva_connect.workflow_engine.wakeups.run_wake_scheduler
#
# (3) is our RQ scheduler for this lane. Frappe hard-disables RQ's scheduler on both worker paths so its
# own is the only one running — correct, and it means a lane Frappe does not manage is an empty lane.
# It is NOT load-bearing: kill it and parked runs still wake off the */15 sweep, late.
LANE_WORKER_COMMAND = f"bench worker --queue {WAKE_QUEUE}"
LANE_SCHEDULER_COMMAND = "bench --site <site> execute tatva_connect.workflow_engine.wakeups.run_wake_scheduler"


def assert_lane_registered():
	"""after_migrate: refuse a site that has ARMED the engine without registering its lane.

	Gated on the engine switch on purpose. The engine ships dormant, so a fresh install and every bench
	that never turned it on are correct with no lane at all — and a `throw` there would fail
	`install-app` itself, since `workers` cannot be set before the app that needs it exists. The moment
	an operator arms the engine, the lane stops being optional and the next migrate says so.
	"""
	if not automation.is_enabled(ENGINE_SWITCH):
		return
	lane = (frappe.conf.get("workers") or {}).get(WAKE_QUEUE)
	if not lane:
		frappe.throw(
			f"The workflow engine is armed but the `{WAKE_QUEUE}` lane is not registered in "
			f"common_site_config `workers`. Timer alarms would be written and silently never execute. "
			f"Register it, then run `{LANE_WORKER_COMMAND}` and `{LANE_SCHEDULER_COMMAND}`."
		)
	if (lane.get("timeout") or 0) < thresholds.WAKE_JOB_TIMEOUT:
		frappe.throw(
			f"The `{WAKE_QUEUE}` lane's timeout is {lane.get('timeout')}s, below the declared "
			f"{thresholds.WAKE_JOB_TIMEOUT}s a wake job may run. A long segment would be killed mid-flight."
		)


def schedule_wake(name, resume_at):
	"""Set the alarm for a parked run. The diary row is already written; this only makes it PUNCTUAL.

	`frappe.enqueue` has no delay parameter, and Frappe hard-disables RQ's scheduler on both worker paths
	(`background_jobs.py:359`, `:364-366`) so that its own scheduler is the only one running. That is a
	reason not to run two schedulers, not a reason to reject delayed work: a queue Frappe's scheduler does
	not manage is an empty lane. `enqueue_at` takes the identical job `enqueue_call` already builds, so
	this mirrors `queue_args` and adds nothing to the queue layer.

	AFTER COMMIT, always: the alarm must not exist for a segment that rolled back. Deduplicated on the run
	name so a re-park cannot stack alarms. The payload is the NAME — `drive_instance` re-reads the row and
	re-claims it, so a stale or duplicated job is a no-op and a lost one is caught by the sweep.
	"""
	from frappe.utils.background_jobs import create_job_id, get_queue

	queue_args = {
		"site": frappe.local.site,
		"user": frappe.session.user,
		"method": "tatva_connect.workflow_engine.wakeups.drive_instance",
		"event": None,
		"job_name": "workflow-wake",
		"is_async": True,
		"kwargs": {"name": name},
	}
	job_id = create_job_id(f"workflow-wake::{name}")
	due = frappe.utils.get_datetime(resume_at)

	def alarm():
		queue = get_queue(WAKE_QUEUE)
		_forget_wake(queue, job_id)
		queue.enqueue_at(
			_as_utc(due),
			"frappe.utils.background_jobs.execute_job",
			kwargs=queue_args,
			job_timeout=thresholds.WAKE_JOB_TIMEOUT,
			job_id=job_id,
		)

	frappe.db.after_commit.add(alarm)


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

	IT IS NOT LOAD-BEARING. Kill it and every run still wakes, late, off the sweep — which is exactly what
	covers the ~61s window while a dead scheduler's lock expires.
	"""
	from frappe.utils.background_jobs import generate_qname, get_redis_conn
	from rq.scheduler import RQScheduler

	RQScheduler([generate_qname(WAKE_QUEUE)], connection=get_redis_conn(), interval=interval).work()


def _forget_wake(queue, job_id):
	"""Drop any alarm already set for this run, so a re-park replaces rather than stacks."""
	from rq.registry import ScheduledJobRegistry

	registry = ScheduledJobRegistry(queue=queue)
	if job_id in registry.get_job_ids():
		registry.remove(job_id, delete_job=True)


def sweep():
	"""The scheduled tick: timer wake, signal backstop, stale-signal purge. Gated on the sweep switch."""
	if not automation.is_enabled(SWEEP_SWITCH):
		return
	timer_sweep()
	reconciler_sweep()
	_purge_stale_signals()


def timer_sweep():
	"""Wake every `Parked` Instance whose clock deadline has arrived, oldest first, capped. Per-row commit."""
	if not automation.is_enabled(SWEEP_SWITCH):
		return
	for name in _due_parked():
		drive_instance(name)
		frappe.db.commit()


def reconciler_sweep():
	"""The reliability backstop (F5): re-drive (a) due-timer Parked rows and (b) Parked rows whose awaited
	signal is already buffered but was never woken (a lost enqueue). Per-row commit. Overlaps timer_sweep on
	(a) BY DESIGN - a re-drive of an already-advanced row is a claimed no-op (F6), never a double-run - so
	the reconciler is a COMPLETE standalone backstop, not dependent on timer_sweep having run first."""
	if not automation.is_enabled(SWEEP_SWITCH):
		return
	for name in _due_parked():
		drive_instance(name)
		frappe.db.commit()
	for row in frappe.get_all(
		INSTANCE_DT,
		filters={"status": "Parked", "awaiting_signal": ["is", "set"]},
		fields=["name", "subject_doctype", "subject_name", "awaiting_signal", "awaiting_correlation"],
		limit=thresholds.SWEEP_PAGE,
	):
		if _has_pending_signal(row):
			drive_instance(row.name)
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


def drive_instance(name):
	"""Claim the Instance `for_update`, re-check it is still `Parked`, and `advance` (F6). Sets
	`in_workflow` so the segment's own writes to the subject don't re-enter entry/signal detection. Plumbing
	failures are logged, never raised, so one bad row never aborts a sweep."""
	frappe.flags.in_workflow = True
	try:
		if not frappe.db.get_value(INSTANCE_DT, {"name": name, "status": "Parked"}, "name", for_update=True):
			return  # already claimed/advanced by another driver, or no longer parked (idempotent)
		interpreter.advance(frappe.get_doc(INSTANCE_DT, name))
	except Exception:
		frappe.log_error(title="workflow: drive failed", message=f"instance={name} :: {frappe.get_traceback()}")
	finally:
		frappe.flags.in_workflow = False


def _due_parked():
	"""Names of `Parked` Instances whose clock deadline has arrived, oldest first, capped."""
	return frappe.get_all(
		INSTANCE_DT,
		filters={"status": "Parked", "resume_at": ["<=", frappe.utils.now_datetime()]},
		order_by="resume_at asc",
		limit=thresholds.SWEEP_PAGE,
		pluck="name",
	)


def _has_pending_signal(row):
	"""True iff a live inbox row matches this Instance's awaited (subject, signal, correlation).

	Asks through `pending_signal_filters`, the ONE description of "a row that would wake this park", so the
	backstop can never re-drive a run on a row the drive itself would then decline to consume. It carried
	its own copy of that dict until W4.4, which is what would have let an EXPIRED row wake a run for ever.
	"""
	filters = interpreter.pending_signal_filters(
		row.subject_doctype, row.subject_name, row.awaiting_signal, row.awaiting_correlation
	)
	return bool(frappe.db.get_value(SIGNAL_DT, filters, "name"))
