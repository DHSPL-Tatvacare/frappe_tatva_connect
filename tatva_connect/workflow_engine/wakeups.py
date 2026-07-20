"""Scheduled wakeups + the reliability backstop.

PRINCIPLE (F5): the durable Instance row is the source of truth; every enqueue is only a latency
optimisation. If a wake job is lost (Redis flush, worker death), the reconciler still drives the Instance
forward from its durable state - nothing depends on an RQ job surviving.

`sweep()` is the ONE scheduled entry (hooks.scheduler_events, ~*/15), double-gated - the master engine
switch AND the sweep switch (so an operator can pause the sweep without killing the engine). It runs, in
order:
  * `timer_sweep` - the TIMER side: `status='Parked' AND resume_at<=now`, claim each `for_update`,
    `advance`, commit PER ROW (a worker killed mid-sweep never replays a segment whose sends already left).
  * `reconciler_sweep` - the reliability backstop: re-drives (a) due-timer Parked rows and (b) a `Parked`
    Instance whose `awaiting_signal` already has a matching Pending inbox row but was never woken (a lost
    enqueue). Overlaps timer_sweep on (a) by design - a re-drive of an already-advanced row is a claimed
    no-op (F6) - so the reconciler is a COMPLETE standalone backstop.
  * `_purge_stale_signals` - keep the inbox bounded (old Consumed + orphan Pending rows).

Every drive claims the Instance `for_update` and re-checks status BEFORE work (F6), and sets
`frappe.flags.in_workflow` so the engine's own writes don't re-enter entry/signal detection.
"""
import frappe

from tatva_connect import automation
from tatva_connect.workflow_engine import ENGINE_SWITCH, SWEEP_SWITCH, interpreter

INSTANCE_DT = interpreter.INSTANCE_DT
SIGNAL_DT = interpreter.SIGNAL_DT
_PAGE_LIMIT = 200  # a sane cap per sweep — a huge backlog drains over several sweeps, not one giant job


def sweep():
	"""The scheduled tick: timer wake, signal backstop, stale-signal purge. Double-gated (engine + sweep)."""
	if not (automation.is_enabled(ENGINE_SWITCH) and automation.is_enabled(SWEEP_SWITCH)):
		return
	timer_sweep()
	reconciler_sweep()
	_purge_stale_signals()


def timer_sweep():
	"""Wake every `Parked` Instance whose clock deadline has arrived, oldest first, capped. Per-row commit."""
	if not (automation.is_enabled(ENGINE_SWITCH) and automation.is_enabled(SWEEP_SWITCH)):
		return
	for name in _due_parked():
		drive_instance(name)
		frappe.db.commit()


def reconciler_sweep():
	"""The reliability backstop (F5): re-drive (a) due-timer Parked rows and (b) Parked rows whose awaited
	signal is already buffered but was never woken (a lost enqueue). Per-row commit. Overlaps timer_sweep on
	(a) BY DESIGN - a re-drive of an already-advanced row is a claimed no-op (F6), never a double-run - so
	the reconciler is a COMPLETE standalone backstop, not dependent on timer_sweep having run first."""
	if not (automation.is_enabled(ENGINE_SWITCH) and automation.is_enabled(SWEEP_SWITCH)):
		return
	for name in _due_parked():
		drive_instance(name)
		frappe.db.commit()
	for row in frappe.get_all(
		INSTANCE_DT,
		filters={"status": "Parked", "awaiting_signal": ["is", "set"]},
		fields=["name", "subject_doctype", "subject_name", "awaiting_signal", "awaiting_correlation"],
		limit=_PAGE_LIMIT,
	):
		if _has_pending_signal(row):
			drive_instance(row.name)
			frappe.db.commit()


def _purge_stale_signals():
	"""Keep the signal inbox bounded (the GC `interpreter._consume_signal` references): drop Consumed rows
	older than a week and any Pending row older than a month (an unclaimed duplicate/orphan). Its own commit;
	harmless when it deletes nothing."""
	now = frappe.utils.now_datetime()
	frappe.db.delete(SIGNAL_DT, {"status": "Consumed", "modified": ["<", frappe.utils.add_to_date(now, days=-7)]})
	frappe.db.delete(SIGNAL_DT, {"status": "Pending", "creation": ["<", frappe.utils.add_to_date(now, days=-30)]})
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
		limit=_PAGE_LIMIT,
		pluck="name",
	)


def _has_pending_signal(row):
	"""True iff a Pending inbox row matches this Instance's awaited (subject, signal, correlation) - the
	same match `_consume_signal` will make on the drive (null correlation matches null/empty)."""
	filters = {"subject_doctype": row.subject_doctype, "subject_name": row.subject_name, "event_name": row.awaiting_signal, "status": "Pending"}
	filters["correlation"] = row.awaiting_correlation if row.awaiting_correlation else ["in", ["", None]]
	return bool(frappe.db.get_value(SIGNAL_DT, filters, "name"))
