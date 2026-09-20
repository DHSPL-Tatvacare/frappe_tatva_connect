"""The `workflow` lane, parked journeys, and the */15 backstop.

The journey row is the source of truth and every job a latency optimisation (F5). `schedule_on_lane` is the engine's
one way to book delayed work, serviced by `run_wake_scheduler`. `wake_due` wakes parked journeys past `resume_at`
for the workflow drain, and `drive_journey` claims one `for_update`, re-checks it is still `Parked` (F6) and
advances it. `sweep` books a drain pass when work is waiting and reaps the signal inbox.
"""
import time

import frappe

from tatva_connect import automation
from tatva_connect.utils import due_now, next_clock_at
from tatva_connect.workflow_engine import ENGINE_SWITCH, interpreter, thresholds

JOURNEY_DT = interpreter.JOURNEY_DT
SIGNAL_DT = interpreter.SIGNAL_DT

# The lane Frappe's own scheduler does not manage, so our RQ scheduler can service it without two
# schedulers fighting. Registered in common_site_config `workers`, exactly as `partner_bulk` is.
WAKE_QUEUE = "workflow"

# THE DEPLOY CONTRACT FOR THIS LANE, in the repo rather than in one machine's compose file.
# Until now it existed ONLY in `.localdev/compose.yml`, which is git-excluded — so the engine ran here
# and nowhere else, and a deploy that missed it would book passes into a queue nothing services.
# That failure is SILENT: every journey parks correctly, every booking is set, and none of them ever runs.
# `assert_lane_registered` below is what makes it loud, and these are the three things it is about:
#
#   1. bench set-config -gp workers "{'workflow': {'background_workers': 1, 'timeout': 1500}}"
#   2. bench worker-pool --queue workflow                                (a pool, one per lane)
#   3. bench --site <site> execute tatva_connect.workflow_engine.wakeups.run_wake_scheduler
#
# (3) is our RQ scheduler for this lane. Frappe hard-disables RQ's scheduler on both worker paths so its
# own is the only one running — correct, and it means a lane Frappe does not manage is an empty lane.
# It is NOT load-bearing: kill it and the */15 backstop still queues a pass, late.
LANE_WORKER_COMMAND = f"bench worker-pool --queue {WAKE_QUEUE}"
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


def schedule_on_lane(due, method, kwargs, key, only_if_earlier=False):
	"""Book ONE job on this lane for a future moment. THE engine's only way to schedule delayed work.

	`frappe.enqueue` has no delay parameter, and Frappe disables RQ's scheduler on its own worker paths so its
	scheduler is the only one running; this lane is one Frappe's scheduler does not manage, so `run_wake_scheduler`
	services it and `enqueue_at` takes the identical job `enqueue_call` builds.

	AFTER COMMIT, always: a booking must not survive the transaction that asked for it rolling back. Each booking
	gets its own job id under the key, so a running job that books its successor never overwrites its own record;
	every earlier booking under the key is dropped first, so a re-book REPLACES rather than stacks.
	`only_if_earlier` keeps a booking already due sooner — how `drain.pull_forward` never pushes a pass back.
	"""
	from frappe.utils.background_jobs import create_job_id, get_queue

	queue = get_queue(WAKE_QUEUE)
	prefix = create_job_id(key)
	at = _as_utc(frappe.utils.get_datetime(due))
	queue_args = {
		"site": frappe.local.site,
		"user": frappe.session.user,
		"method": method,
		"event": None,
		"job_name": key,
		"is_async": True,
		"kwargs": kwargs,
	}

	def book():
		held = _bookings(queue, prefix)
		if only_if_earlier and any(when <= at for when in held.values()):
			return
		_forget(queue, held)
		queue.enqueue_at(
			at,
			"frappe.utils.background_jobs.execute_job",
			kwargs=queue_args,
			job_timeout=thresholds.WAKE_JOB_TIMEOUT,
			job_id=f"{prefix}@{int(at.timestamp() * 1000)}",
		)

	frappe.db.after_commit.add(book)


def forget_on_lane(key):
	"""Drop every booking held under this key, after commit. The other half of `schedule_on_lane`."""
	from frappe.utils.background_jobs import create_job_id, get_queue

	queue = get_queue(WAKE_QUEUE)
	prefix = create_job_id(key)
	frappe.db.after_commit.add(lambda: _forget(queue, _bookings(queue, prefix)))


def _as_utc(due):
	"""RQ schedules in UTC; `resume_at` is written in the site's timezone. Converted once, here, because a
	timezone bug in a booking is invisible until a patient is messaged at the wrong hour."""
	from datetime import timezone
	from zoneinfo import ZoneInfo

	if due.tzinfo is None:
		due = due.replace(tzinfo=ZoneInfo(frappe.utils.get_system_timezone()))
	return due.astimezone(timezone.utc)


def run_wake_scheduler(interval=1):
	"""Service the bookings on the workflow lane. Runs as its own process, forever.

	Frappe starts its own scheduler inside `FrappeWorker` and passes `with_scheduler=False` to RQ so the
	two cannot both run. This lane is not one Frappe's scheduler manages, so an RQ scheduler here services
	due bookings without contending with it. The W4 spike found a standalone process far easier to observe
	than the one `Worker.work(with_scheduler=True)` forks, and the per-queue Redis lock (TTL =
	interval + 60) already guarantees only one is ever moving jobs.

	IT IS NOT LOAD-BEARING. Kill it and the */15 backstop still queues a pass, late — which is exactly what covers
	the ~61s window while a dead scheduler's lock expires.
	"""
	from frappe.utils.background_jobs import generate_qname, get_redis_conn
	from rq.scheduler import RQScheduler

	RQScheduler([generate_qname(WAKE_QUEUE)], connection=get_redis_conn(), interval=interval).work()


def _bookings(queue, prefix):
	"""`{job_id: UTC time}` for every booking held under a key — RQ's own scheduled registry, asked, never modelled."""
	from datetime import timezone

	from rq.registry import ScheduledJobRegistry

	registry = ScheduledJobRegistry(queue=queue)
	held = {}
	for job_id in registry.get_job_ids():
		if job_id == prefix or job_id.startswith(f"{prefix}@"):
			when = registry.get_scheduled_time(job_id)
			held[job_id] = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
	return held


def _forget(queue, held):
	"""Drop these bookings from the lane's scheduled registry, with their job records."""
	from rq.registry import ScheduledJobRegistry

	registry = ScheduledJobRegistry(queue=queue)
	for job_id in held:
		registry.remove(job_id, delete_job=True)


def sweep():
	"""The */15 backstop: book a drain pass if work is waiting, so a lost booking costs one sweep, then reap the inbox."""
	from tatva_connect.workflow_engine import drain

	if not automation.is_enabled(ENGINE_SWITCH):
		return
	if drain.has_work():
		drain.kick()
		frappe.db.commit()
	_purge_stale_signals()


def wake_due(limit, until, renew):
	"""Wake up to `limit` due journeys, oldest deadline first, stopping at `until`. Returns how many it drove."""
	woken = 0
	for name in due_journeys(limit=limit):
		if time.monotonic() >= until:
			break
		renew()
		# Each claim reads its own snapshot, as `spine._work` does: a stale one is what raises 1020 on the locking read.
		frappe.db.commit()
		drive_journey(name, skip_locked=True)
		frappe.db.commit()
		woken += 1
	return woken


def next_resume_at():
	"""The earliest deadline a parked journey is still waiting for, or None."""
	return next_clock_at(JOURNEY_DT, "resume_at", filters=[["status", "=", "Parked"]])


def _purge_stale_signals():
	"""W4.4 — THE reaper: age the dead out of the inbox, then delete what has been terminal long enough.

	A row nothing ever claimed used to sit `Pending` for a month and then be DELETED. It reached no
	terminal state, so the inbox could not say what became of it; and all month it could still be claimed
	by a park — a message going out on a weeks-old signal the moment sends are armed.

	So expiry is a STATE. `Expired` is terminal, and inert because `pending_signal_filters` and `signals.pending`
	ask for `PENDING` and nothing else. One reaper, not two: this function already owned this table's age policy.
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


def drive_journey(name, skip_locked=False):
	"""Claim the Journey `for_update`, re-check it is still `Parked`, and `advance` (F6). Sets
	`in_workflow` so the segment's own writes to the subject don't re-enter entry/signal detection. Plumbing
	failures are logged, never raised, so one bad row never aborts a pass. `skip_locked` is the drain's claim:
	a journey another driver holds is theirs, and the pass moves on rather than queueing behind it."""
	frappe.flags.in_workflow = True
	try:
		claimed = frappe.db.get_value(
			JOURNEY_DT, {"name": name, "status": "Parked"}, ["name", "workflow"], as_dict=True, for_update=True,
			skip_locked=skip_locked,
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


def due_journeys(limit=None):
	"""Names of `Parked` Journeys whose clock deadline has arrived, oldest first, capped."""
	return due_now(
		JOURNEY_DT,
		"resume_at",
		filters=[["status", "=", "Parked"]],
		order_by="resume_at asc",
		limit=limit or thresholds.SWEEP_PAGE,
		pluck="name",
	)
