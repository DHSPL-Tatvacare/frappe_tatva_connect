# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Lead routing (DA54, DA57): untouched-lead limits, waiting leads woken by events, and untouched leads reassigned."""
import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_datetime, now_datetime
from frappe.utils.data import evaluate_filters

from tatva_connect import automation
from tatva_connect.notifications import batch
from tatva_connect.utils import read_committed
from tatva_connect.workflow_engine import ENGINE_SWITCH, as_system

LEAD, TASK, CALL_LOG = "CRM Lead", "CRM Task", "CRM Call Log"
# A lead reassigned this many times stays with its last holder, unless its Distribute node sets its own limit.
MAX_REASSIGNS = 3
# A task of the holder's still in these statuses is untouched; the kinds of work that end it are `lead_work`.
UNTOUCHED = ("Backlog", "Todo")
OUTBOUND_CALL = "Outgoing"
# The pool's waiting order that reverses the queue; blank and "Oldest first" go oldest first.
NEWEST_FIRST = "Newest first"
_DRIVE = "tatva_connect.lead.routing.drive_parked"
_REASSIGN = "tatva_connect.lead.routing.reassign_if_untouched"


def wake_pool(pool):
	"""Book one pass over the pool's waiting leads now, after commit; a booking already due keeps it, so a burst of wakes makes one job."""
	from tatva_connect.workflow_engine import interpreter, wakeups

	if frappe.db.exists(interpreter.JOURNEY_DT, _waiting(pool)):
		wakeups.schedule_on_lane(now_datetime(), _DRIVE, {"pool": pool}, f"pool:{pool}", only_if_earlier=True)


def drive_parked(pool):
	"""The pass: drive each journey waiting on the pool in the pool's order (oldest first unless it says newest); each re-enters Distribute and draws again, or waits again."""
	as_system()
	read_committed()
	from tatva_connect.workflow_engine import interpreter, wakeups

	if not automation.is_enabled(ENGINE_SWITCH):
		return
	newest = frappe.db.get_value("Assignment Rule", pool, "waiting_order") == NEWEST_FIRST
	for name in frappe.get_all(interpreter.JOURNEY_DT, filters=_waiting(pool), order_by=f"creation {'desc' if newest else 'asc'}", pluck="name"):
		# Each claim reads its own snapshot, as `wakeups.wake_due` does.
		frappe.db.commit()
		wakeups.drive_journey(name, skip_locked=True)
		frappe.db.commit()
		# A lead that waits again means nobody has room; the leads behind it would only wait again too.
		if frappe.db.exists(interpreter.JOURNEY_DT, {"name": name, **_waiting(pool)}):
			break


def wake_pools_for(user):
	"""A rep checked in: wake every enabled pool that requires check-in and lists them."""
	from tatva_connect.lead import checkin

	for pool in sorted({row.parent for row in checkin._member_rows(user)}):
		wake_pool(pool)


def open_shifts():
	"""Runs at each shift start (`checkin.sync_shift_jobs`): ask the reps now on shift to check in, and wake every pool with waiting leads that is open now."""
	from tatva_connect.lead import checkin

	checkin.ask_at_shift_open()
	wake_open_pools()


def wake_open_pools():
	"""Wake every open pool with waiting leads: at a shift's opening, and from the */15 sweep for a pass that was lost (a restart, or killed at its job limit)."""
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
	"""Leads the pool gave `user` that they still hold and have not worked."""
	return untouched_counts(pool, [user]).get(user, 0)


def untouched_counts(pool, users):
	"""`untouched_count` for many reps at once."""
	counts = {}
	for user, _lead in untouched_pairs(pool, users):
		counts[user] = counts.get(user, 0) + 1
	return counts


def untouched_pairs(pool, users, leads=None):
	"""`(user, lead)` the pool gave `users` that they still hold and have not worked: every task of theirs on it still Backlog or Todo, and none of their `lead_work` on it."""
	if not users:
		return set()
	# This pool's open ToDos joined to the holder's untouched tasks: ToDo (allocated_to, assignment_rule, creation), then CRM Task by reference_docname.
	todo, task = frappe.qb.DocType("ToDo"), frappe.qb.DocType(TASK)
	query = (
		frappe.qb.from_(todo).join(task).on((task.reference_docname == todo.reference_name) & (task.assigned_to == todo.allocated_to))
		.select(todo.allocated_to, todo.reference_name, todo.creation).distinct()
		.where(todo.allocated_to.isin(list(users)) & (todo.assignment_rule == pool) & (todo.status == "Open") & (todo.reference_type == LEAD))
		.where((task.reference_doctype == LEAD) & task.status.isin(UNTOUCHED))
	)
	if leads:
		query = query.where(todo.reference_name.isin(list(leads)))
	held = {(r.allocated_to, r.reference_name): r.creation for r in query.run(as_dict=True)}
	if not held:
		return set()
	names = list({lead for _user, lead in held})
	worked = set()
	for doctype, work in lead_work().items():
		for row in frappe.get_all(doctype, filters={**work["worked"], work["lead"]: ["in", names], work["by"]: ["in", list(users)]},
		                          fields=[f"{work['by']} as user", f"{work['lead']} as lead", "creation"]):
			pair = (row.user, row.lead)
			if pair in held and (not work["since_held"] or row.creation >= held[pair]):
				worked.add(pair)
	return set(held) - worked


def lead_work():
	"""What counts as working a lead a pool gave a rep, by doctype: the lead's field, who did it, the filters that mean worked, and whether only work after they got the lead counts; a new kind is one entry here plus its on_update line in hooks.py (frappe runs on_update on insert too), locked by a test."""
	return {
		TASK: {"lead": "reference_docname", "by": "assigned_to", "worked": {"reference_doctype": LEAD, "status": ["not in", UNTOUCHED]}, "since_held": False},
		CALL_LOG: {"lead": "reference_docname", "by": "caller", "worked": {"reference_doctype": LEAD, "type": OUTBOUND_CALL}, "since_held": True},
	}


def movable(rule):
	"""Would the move act on this pool's leads at all: the engine is on and the pool is Credit Weighted; the move and its warning both ask."""
	from tatva_connect.lead.assignment_rule import CREDIT_WEIGHTED

	return automation.is_enabled(ENGINE_SWITCH) and rule.rule == CREDIT_WEIGHTED


def on_lead_work(doc, method=None):
	"""on_update of every `lead_work` doctype (insert included): a row that just became a rep's work on a lead changes their card and gives the pools that gave them the lead room; wake those pools' waiting leads."""
	work = lead_work().get(doc.doctype)
	if not work or not _is_work(doc, work):
		return
	before = doc.get_doc_before_save()
	if before and _is_work(before, work) and before.get(work["by"]) == doc.get(work["by"]):
		return  # counted when it first became their work
	from tatva_connect.lead import checkin

	user, lead = doc.get(work["by"]), doc.get(work["lead"])
	checkin.tell_card(user)  # a lead just worked may lift the rep's untouched-limit pause
	if not automation.is_enabled(ENGINE_SWITCH):
		return
	pools = frappe.get_all(
		"ToDo",
		filters={"reference_type": LEAD, "reference_name": lead, "allocated_to": user, "status": "Open", "assignment_rule": ["is", "set"]},
		pluck="assignment_rule",
	)
	for pool in set(pools):
		wake_pool(pool)


def _is_work(doc, work):
	return bool(doc.get(work["by"]) and doc.get(work["lead"])) and evaluate_filters(doc, work["worked"])


def book_reassign(journey, lead, seconds, at=None, limit=None):
	"""Save on the journey when to check the lead (and, on the first booking, its node's move limit) and book that job; the 15-minute sweep runs it if Redis loses the job."""
	from tatva_connect.workflow_engine import interpreter, wakeups

	seconds = cint(seconds)
	if not journey or seconds <= 0:
		return
	# Whole seconds, as RQ keeps a job's time: a fraction would let the job wake a moment early and find the check not yet due.
	at = get_datetime(at or add_to_date(now_datetime(), seconds=seconds)).replace(microsecond=0)
	values = {"reassign_at": at, "reassign_after": seconds, **({"reassign_limit": limit} if limit else {})}
	frappe.db.set_value(interpreter.JOURNEY_DT, journey, values, update_modified=False)  # authz-ok: tier-a — workflow engine, queue context
	wakeups.schedule_on_lane(at, _REASSIGN, {"journey": journey}, f"reassign:{lead}")


def reassign_if_untouched(journey):
	"""Move the lead to the pool's next rep if its rep has not worked it, at most its node's limit (`MAX_REASSIGNS` when blank) times."""
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
	from tatva_connect.workflow_engine import interpreter

	# Locks the journey row, so the job and the sweep never both move the lead.
	due = frappe.db.get_value(interpreter.JOURNEY_DT, journey, ["subject_name", "reassign_at", "reassign_after", "reassign_limit", "batch_doctype", "batch_name"], as_dict=True, for_update=True)
	if not (due and due.reassign_at and due.reassign_at <= now_datetime()):
		return
	lead, seconds = due.subject_name, due.reassign_after
	# Locks the rep's ToDo, so a manual reassign at the same moment waits for this check.
	held = frappe.db.get_value(
		"ToDo", {"reference_type": LEAD, "reference_name": lead, "status": "Open", "assignment_rule": ["is", "set"]},
		["allocated_to", "assignment_rule"], as_dict=True, for_update=True,
	)
	if not held or assignment.current_assignees(LEAD, lead) != [held.allocated_to] or not untouched_pairs(held.assignment_rule, [held.allocated_to], [lead]):
		return _stop_reassign(journey)
	pool = held.assignment_rule
	limit = cint(due.reassign_limit) or MAX_REASSIGNS
	if _reassigns(pool, lead) >= limit or not frappe.db.exists("Assignment Rule", pool):
		return _stop_reassign(journey)
	rule = frappe.get_doc("Assignment Rule", pool)
	if not movable(rule):
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
	with batch.holding(due.batch_doctype, due.batch_name):  # a hand-off is a step of the journey, so its bulk action tells it
		assignment.assign_for_system(LEAD, lead, user, replace=True, note=note, assignment_rule=pool)
		_tell_moved(held.allocated_to, lead, doc.lead_name)
	# The owner moves with the lead only when the pool chose it, so an owner a person chose stays.
	if doc.lead_owner == held.allocated_to and automation.is_enabled(assignment.OWNER_FROM_RULE):
		assignment.move_owner(lead, user, displaced=True)
	if _reassigns(pool, lead) >= limit:
		return _stop_reassign(journey)
	book_reassign(journey, lead, seconds)


def reassign_due():
	"""Run every past-due reassign check, oldest first, each committing on its own so one failure never stops the rest."""
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


def _tell_moved(holder, lead, label):
	"""The rep the lead was taken from hears it once, or once per wave inside a batch, as an assignment is told; a failed notice never undoes the move."""
	from tatva_connect.notifications import dispatch

	frappe.db.savepoint("lead_moved_notice")
	try:
		if batch.covers():
			return batch.count(holder, LEAD, lead, kind=dispatch.LEAD_MOVED)
		dispatch.notify(dispatch.LEAD_MOVED, [holder], actor="Administrator", text=_("Moved to another rep, not worked in time: {0}").format(label or lead),
		                source=(LEAD, lead), target=(LEAD, lead))
	except Exception:
		frappe.db.rollback(save_point="lead_moved_notice")
		frappe.log_error(title="routing: lead moved notice failed", reference_doctype=LEAD, reference_name=lead)


def _reassigns(pool, lead):
	"""How often the pool's lead changed hands: its ToDos from this pool that were cancelled. Derived, never stored."""
	return frappe.db.count("ToDo", {"reference_type": LEAD, "reference_name": lead, "assignment_rule": pool, "status": "Cancelled"})


def _waiting(pool=None):
	"""Runs parked in a Distribute step until someone in `pool` (any pool when None) can take their record."""
	return {"status": "Parked", "awaiting_signal": _signal(), **({"awaiting_correlation": pool} if pool else {})}


def _signal():
	from tatva_connect.automation.actions import POOL_SIGNAL

	return POOL_SIGNAL
