# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Lead routing (DA54, DA57): a rep holds at most a pool's limit of untouched leads, where the pool sets one, waiting leads are woken by events, and an untouched lead can be reassigned. Every routing rule lives here.

A waiting lead is a journey `Parked` inside Distribute on `POOL_SIGNAL`, correlated by the pool; only a node that ticks
Wait for a rep to check in parks one. Eligibility, shifts, leave, check-in and the credit draw stay in `TatvaAssignmentRule`.
Every job is booked on the `workflow` lane by an event (a check-in, a shift start, an assignment); nothing polls.
A reassign check is saved on the journey (`reassign_at`), so the */15 sweep runs one whose job Redis lost.
"""
import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from tatva_connect import automation
from tatva_connect.workflow_engine import ENGINE_SWITCH, as_system

LEAD, TASK = "CRM Lead", "CRM Task"
# A lead reassigned this many times stays with its last holder.
MAX_REASSIGNS = 3
# A task still in one of these is untouched: the rep has not started or finished it.
UNTOUCHED = ("Backlog", "Todo")
_DRIVE = "tatva_connect.lead.routing.drive_parked"
_REASSIGN = "tatva_connect.lead.routing.reassign_if_untouched"


def wake_pool(pool):
	"""Book one pass over the pool's waiting leads now, after commit; a booking already due keeps it, so a burst of wakes makes one job."""
	from tatva_connect.workflow_engine import interpreter, wakeups

	if frappe.db.exists(interpreter.JOURNEY_DT, _waiting(pool)):
		wakeups.schedule_on_lane(now_datetime(), _DRIVE, {"pool": pool}, f"pool:{pool}", only_if_earlier=True)


def drive_parked(pool):
	"""The pass: drive each journey waiting on the pool, oldest first; each re-enters Distribute and draws again, or waits again."""
	as_system()
	from tatva_connect.workflow_engine import interpreter, wakeups

	if not automation.is_enabled(ENGINE_SWITCH):
		return
	for name in frappe.get_all(interpreter.JOURNEY_DT, filters=_waiting(pool), order_by="creation asc", pluck="name"):
		# Each claim reads its own snapshot, as `wakeups.wake_due` does.
		frappe.db.commit()
		wakeups.drive_journey(name, skip_locked=True)
		frappe.db.commit()
		# A lead that waits again means nobody has room; the younger leads behind it would only wait again too.
		if frappe.db.exists(interpreter.JOURNEY_DT, {"name": name, **_waiting(pool)}):
			break


def wake_pools_for(user):
	"""A rep checked in: wake every enabled pool that requires check-in and lists them."""
	from tatva_connect.lead import checkin

	for pool in sorted({row.parent for row in checkin._member_rows(user)}):
		wake_pool(pool)


def open_shifts():
	"""Runs at each shift start (`checkin.sync_shift_jobs`): wake every pool with waiting leads that is open now."""
	from tatva_connect.workflow_engine import interpreter

	pools = frappe.get_all(
		interpreter.JOURNEY_DT, filters={"status": "Parked", "awaiting_signal": _signal()}, pluck="awaiting_correlation", distinct=True
	)
	for pool in pools:
		rule = frappe.get_cached_doc("Assignment Rule", pool) if pool and frappe.db.exists("Assignment Rule", pool) else None
		if rule and not rule.disabled and rule.open_window()[0]:
			wake_pool(pool)


def has_room(rule, user):
	"""Does `user` hold fewer untouched leads from this pool than its limit; a pool with no limit set always has room (DA57)."""
	limit = cint(rule.get("max_untouched"))
	return not limit or untouched_count(rule.name, user) < limit


def untouched_count(pool, user):
	"""Leads the pool gave `user` that they still hold with a task of theirs on it still Backlog or Todo."""
	leads = frappe.get_all(
		"ToDo", filters={"reference_type": LEAD, "assignment_rule": pool, "allocated_to": user, "status": "Open"}, pluck="reference_name"
	)
	if not leads:
		return 0
	return len(set(frappe.get_all(
		TASK,
		filters={"reference_doctype": LEAD, "reference_docname": ["in", leads], "assigned_to": user, "status": ["in", UNTOUCHED]},
		pluck="reference_docname",
	)))


def on_task_update(task, method=None):
	"""CRM Task.on_update: a rep started or closed a task on a lead a pool gave them, so that pool may have room; wake its waiting leads."""
	if not automation.is_enabled(ENGINE_SWITCH):
		return
	before = task.get_doc_before_save()
	if task.reference_doctype != LEAD or task.status in UNTOUCHED or not before or before.status not in UNTOUCHED:
		return
	pools = frappe.get_all(
		"ToDo",
		filters={"reference_type": LEAD, "reference_name": task.reference_docname, "allocated_to": task.assigned_to, "status": "Open", "assignment_rule": ["is", "set"]},
		pluck="assignment_rule",
	)
	for pool in set(pools):
		wake_pool(pool)


def book_reassign(journey, lead, seconds, at=None):
	"""Save on the journey when to check the lead, `seconds` from now or at `at`, and book a job for that time.

	If Redis loses the job, the 15-minute sweep runs the check instead (`reassign_due`)."""
	from tatva_connect.workflow_engine import interpreter, wakeups

	seconds = cint(seconds)
	if not journey or seconds <= 0:
		return
	at = at or add_to_date(now_datetime(), seconds=seconds)
	frappe.db.set_value(interpreter.JOURNEY_DT, journey, {"reassign_at": at, "reassign_after": seconds}, update_modified=False)  # authz-ok: tier-a — workflow engine, queue context
	wakeups.schedule_on_lane(at, _REASSIGN, {"journey": journey}, f"reassign:{lead}")


def reassign_if_untouched(journey):
	"""Move the lead to the next rep in the pool's draw if its rep has not started their task on it.

	The checks stop when the rep starts the task, someone reassigns the lead by hand, or the lead has moved 3 times.
	If the pool is closed, the check moves to its next opening. If no other rep can take the lead, it runs again after the same delay."""
	as_system()
	if not automation.is_enabled(ENGINE_SWITCH):
		return
	# A hand-off is the engine's own write, as a draw inside a journey is, so it starts no other workflow.
	frappe.flags.in_workflow = True
	try:
		_reassign(journey)
	finally:
		frappe.flags.in_workflow = False


def _reassign(journey):
	from tatva_connect.lead import assignment
	from tatva_connect.lead.assignment_rule import CREDIT_WEIGHTED
	from tatva_connect.workflow_engine import interpreter

	# Locks the journey row, so the job and the sweep never both move the lead.
	due = frappe.db.get_value(interpreter.JOURNEY_DT, journey, ["subject_name", "reassign_at", "reassign_after"], as_dict=True, for_update=True)
	if not (due and due.reassign_at and due.reassign_at <= now_datetime()):
		return
	lead, seconds = due.subject_name, due.reassign_after
	# Locks the rep's ToDo, so a manual reassign at the same moment waits for this check.
	held = frappe.db.get_value(
		"ToDo", {"reference_type": LEAD, "reference_name": lead, "status": "Open", "assignment_rule": ["is", "set"]},
		["allocated_to", "assignment_rule"], as_dict=True, for_update=True,
	)
	if not held or assignment.current_assignees(LEAD, lead) != [held.allocated_to] or not _untouched(lead, held.allocated_to):
		return _stop_reassign(journey)
	pool = held.assignment_rule
	if _reassigns(pool, lead) >= MAX_REASSIGNS or not frappe.db.exists("Assignment Rule", pool):
		return _stop_reassign(journey)
	rule = frappe.get_doc("Assignment Rule", pool)
	if rule.rule != CREDIT_WEIGHTED:
		return _stop_reassign(journey)
	is_open, opens_at = rule.open_window() if rule.keeps_hours() else (True, None)
	if not is_open:
		# If no shift will open the pool, the checks stop.
		return book_reassign(journey, lead, seconds, at=opens_at) if opens_at else _stop_reassign(journey)
	doc = frappe.get_doc(LEAD, lead).as_dict()
	user = None if rule.is_rule_not_applicable_today() else rule.get_credit_weighted_user(doc, exclude=held.allocated_to)
	if not user:
		return book_reassign(journey, lead, seconds)
	# Written as core's `do_assignment` writes a pick; `tasks.on_lead_reassignment_handover` moves the open tasks with it.
	note = frappe.render_template(rule.description, doc) if rule.description else _("Reassigned: the lead was not touched in time")
	assignment.assign(LEAD, lead, user, replace=True, note=note, ignore_permissions=True, assignment_rule=pool)
	if _reassigns(pool, lead) >= MAX_REASSIGNS:
		return _stop_reassign(journey)
	book_reassign(journey, lead, seconds)


def reassign_due():
	"""Run every check that is past due, oldest first. These are the checks whose job never ran.

	Each check commits on its own, as `wakeups.wake_due` drives each journey, so a failed check is logged and the rest still run."""
	from tatva_connect.utils import due_now
	from tatva_connect.workflow_engine import interpreter, thresholds

	for journey in due_now(interpreter.JOURNEY_DT, "reassign_at", order_by="reassign_at asc", limit=thresholds.SWEEP_PAGE, pluck="name"):
		frappe.db.commit()
		try:
			reassign_if_untouched(journey)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title="routing: reassign check failed", reference_doctype=interpreter.JOURNEY_DT, reference_name=journey)


def _stop_reassign(journey):
	"""Clear the check, so it never runs again."""
	from tatva_connect.workflow_engine import interpreter

	frappe.db.set_value(interpreter.JOURNEY_DT, journey, "reassign_at", None, update_modified=False)  # authz-ok: tier-a — workflow engine, queue context


def _untouched(lead, holder):
	"""`holder` has a CRM Task on the lead, and every one is still Backlog or Todo."""
	tasks = frappe.get_all(
		TASK, filters={"reference_doctype": LEAD, "reference_docname": lead, "assigned_to": holder},
		pluck="status",
	)
	return bool(tasks) and all(status in UNTOUCHED for status in tasks)


def _reassigns(pool, lead):
	"""How often the pool's lead changed hands: its ToDos from this pool that were cancelled. Derived, never stored."""
	return frappe.db.count("ToDo", {"reference_type": LEAD, "reference_name": lead, "assignment_rule": pool, "status": "Cancelled"})


def _waiting(pool):
	return {"status": "Parked", "awaiting_signal": _signal(), "awaiting_correlation": pool}


def _signal():
	from tatva_connect.automation.actions import POOL_SIGNAL

	return POOL_SIGNAL
