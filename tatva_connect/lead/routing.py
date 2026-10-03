# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Lead routing (DA54, DA55): a rep holds at most a pool's limit of untouched leads, waiting leads are woken by events, and an untouched lead can be reassigned. Every routing rule lives here.

A waiting lead is a journey `Parked` inside Distribute on `POOL_SIGNAL`, correlated by the pool; only a node that ticks
Wait for a rep to check in parks one. Eligibility, shifts, leave, check-in and the credit draw stay in `TatvaAssignmentRule`.
Every job is booked on the `workflow` lane by an event (a check-in, a shift start, an assignment); nothing polls.
"""
import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from tatva_connect import automation
from tatva_connect.workflow_engine import ENGINE_SWITCH

LEAD, TASK = "CRM Lead", "CRM Task"
# A lead reassigned this many times stays with its last holder.
MAX_REASSIGNS = 3
# Untouched leads a rep may hold from one pool until a manager sets another limit; the limit is never off (DA55).
DEFAULT_MAX_UNTOUCHED = 3
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
	from tatva_connect.workflow_engine import interpreter, wakeups

	if not automation.is_enabled(ENGINE_SWITCH):
		return
	_as_system()
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


def max_untouched(rule):
	"""The pool's limit on untouched leads per rep: its own setting, else the default; never below one."""
	return max(cint(rule.get("max_untouched")) or DEFAULT_MAX_UNTOUCHED, 1)


def has_room(rule, user):
	"""Does `user` hold fewer untouched leads from this pool than its limit."""
	return untouched_count(rule.name, user) < max_untouched(rule)


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


def book_reassign(lead, pool, holder, seconds):
	"""Book the reassign check for a lead the pool just gave `holder`, `seconds` from now; none once it was reassigned `MAX_REASSIGNS` times."""
	from tatva_connect.workflow_engine import wakeups

	seconds = cint(seconds)
	if seconds <= 0 or _reassigns(pool, lead) >= MAX_REASSIGNS:
		return
	kwargs = {"lead": lead, "pool": pool, "holder": holder, "seconds": seconds}
	wakeups.schedule_on_lane(add_to_date(now_datetime(), seconds=seconds), _REASSIGN, kwargs, f"reassign:{lead}")


def reassign_if_untouched(lead, pool, holder, seconds):
	"""The reassign timer: a lead `holder` alone still holds, every task of theirs on it untouched, goes to the next rep the pool's credit draw gives."""
	from tatva_connect.lead import assignment
	from tatva_connect.lead.assignment_rule import CREDIT_WEIGHTED

	if not automation.is_enabled(ENGINE_SWITCH):
		return
	_as_system()
	# Locks the holder's assignment: a hand reassignment racing this either waits for it or has already cancelled it.
	held = frappe.db.get_value(
		"ToDo", {"reference_type": LEAD, "reference_name": lead, "allocated_to": holder, "status": "Open"}, "name", for_update=True
	)
	if not held or assignment.current_assignees(LEAD, lead) != [holder] or not _untouched(lead, holder):
		return
	if _reassigns(pool, lead) >= MAX_REASSIGNS or not frappe.db.exists("Assignment Rule", pool):
		return
	rule = frappe.get_doc("Assignment Rule", pool)
	if rule.rule != CREDIT_WEIGHTED or rule.is_rule_not_applicable_today():
		return
	doc = frappe.get_doc(LEAD, lead).as_dict()
	if not (user := rule.get_credit_weighted_user(doc, exclude=holder)):
		return
	# Written as core's `do_assignment` writes a pick; `tasks.on_lead_reassignment_handover` moves the open tasks with it.
	note = frappe.render_template(rule.description, doc) if rule.description else _("Reassigned: the lead was not touched in time")
	assignment.assign(LEAD, lead, user, replace=True, note=note, ignore_permissions=True, assignment_rule=pool)
	book_reassign(lead, pool, user, seconds)


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


def _as_system():
	"""A routing job acts for the pool, not for whoever's event booked it, so the steps it drives never read as a person's choice."""
	frappe.set_user("Administrator")
