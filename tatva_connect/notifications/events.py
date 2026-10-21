"""Notification triggers: each asks `dispatch.armed` first, then resolves WHO + WHAT for `dispatch.notify`; task reminders are a 5-minute cron, one notice per rep."""
from collections import defaultdict
from urllib.parse import quote

import frappe
from crm.api.doc import get_assigned_users
from frappe import _
from frappe.utils import add_to_date, now_datetime

from tatva_connect.notifications import api, dispatch
from tatva_connect.notifications.api import LEAD_FIELD, USER_SETTINGS
from tatva_connect.notifications.dispatch import LEAD_MOVING_SOON, MISSED_CALL, TASK_DUE_SOON, TASK_OVERDUE
from tatva_connect.propagate import fail_safe
from tatva_connect.tasks.tasks import open_statuses

# One run reads at most this many tasks, oldest first; the rest are told by the next run.
_RUN_CAP = 5000

# The oldest overdue task still worth telling, so arming the switch never announces the whole backlog.
_OVERDUE_FLOOR_MINUTES = 7 * 1440


def _assignees(doctype, name) -> list:
	"""Who is told: crm's own resolver, falling back to `lead_owner` for an unassigned lead, as automation/actions.py does."""
	if not doctype or not name:
		return []
	owner = frappe.db.get_value("CRM Lead", name, "lead_owner") if doctype == "CRM Lead" else None
	return list(get_assigned_users(doctype, name, default_assigned_to=owner) or [])


# Doc events fire once; @fail_safe keeps a refused alert from taking the save (lead assignment and inbound WhatsApp archived in .archive/notify-one-seam-2026-10-19: frappe and crm write those rows).
def on_call_missed(doc, method=None):
	"""CRM Call Log on_update: only the save that MOVES an inbound call to No Answer, never a later save of it (`get_doc_before_save`, not `has_value_changed`)."""
	if doc.get("type") != "Incoming" or doc.get("status") != "No Answer":
		return
	before = doc.get_doc_before_save()
	if before and before.get("status") != doc.get("status") and dispatch.armed():
		_call_missed(doc)


@fail_safe
def _call_missed(doc, method=None):
	lead = doc.get("reference_docname") if doc.get("reference_doctype") == "CRM Lead" else None
	if not lead:
		return
	dispatch.notify(
		MISSED_CALL,
		_assignees("CRM Lead", lead),
		actor=doc.owner,
		text=_("Missed call from {0}").format(doc.get("from") or _("a lead")),
		source=("CRM Call Log", doc.name),
		target=("CRM Lead", lead),
	)


# Schedule: a due date passing fires nothing, so a 5-minute cron tells each rep once per reminder type, however many tasks they have.
_REMINDERS = {
	TASK_DUE_SOON: ("custom_due_soon_notified_for", "Due soon: {0}"),
	TASK_OVERDUE: ("custom_overdue_notified_for", "Overdue: {0}"),
}
REMINDER_STAMPS = tuple(stamp for stamp, _phrase in _REMINDERS.values())
_TASK_LIST, _LEAD_LIST = ("CRM Task", None), ("CRM Lead", None)


def _leads(reps, field=LEAD_FIELD, default=api.LEAD_DEFAULT_MINUTES) -> dict:
	"""{rep: minutes} from each rep's own row, in one read; a rep with none set gets the default."""
	rows = frappe.get_all(USER_SETTINGS, filters={"name": ["in", reps]}, fields=["name", field])
	chosen = {r.name: api.minutes(r.get(field)) for r in rows}
	return {rep: chosen.get(rep) or default for rep in reps}


def sweep_reminders():
	"""Every 5 minutes, while the switch is on: each reminder, one notice per rep."""
	if not dispatch.armed():
		return
	for bell_type in _REMINDERS:
		_remind(bell_type)
	_remind_moving()


def _remind_moving():
	"""Leads whose move check falls inside their holder's warning time and was not told yet; the holder still holds them unworked; one notice per rep."""
	from tatva_connect.lead import routing
	from tatva_connect.workflow_engine import interpreter

	now = now_datetime()
	longest = max(filter(None, map(api.minutes, api.lead_options(api.MOVING_LEAD_FIELD))), default=api.MOVING_DEFAULT_MINUTES)
	Journey = frappe.qb.DocType(interpreter.JOURNEY_DT)
	due = (
		frappe.qb.get_query(interpreter.JOURNEY_DT, fields=["name", "subject_name", "reassign_at"],
		                    filters=[["reassign_at", ">", now], ["reassign_at", "<=", add_to_date(now, minutes=longest)]])
		.where(Journey.moving_soon_told_for.isnull() | (Journey.moving_soon_told_for != Journey.reassign_at))
		.orderby(Journey.reassign_at).limit(_RUN_CAP).run(as_dict=True)
	)
	if not due:
		return
	held = frappe.get_all("ToDo", filters={"reference_type": "CRM Lead", "reference_name": ["in", [j.subject_name for j in due]], "status": "Open"},
	                      fields=["reference_name", "allocated_to", "assignment_rule"])
	# The move's own preconditions: one holder in all, given by a pool the move acts on.
	counts = defaultdict(int)
	for r in held:
		counts[r.reference_name] += 1
	holder = {r.reference_name: r for r in held if counts[r.reference_name] == 1 and r.assignment_rule}
	unworked = set()
	for pool in {r.assignment_rule for r in holder.values()}:
		if not frappe.db.exists("Assignment Rule", pool) or not routing.movable(frappe.get_cached_doc("Assignment Rule", pool)):
			continue
		rows = [r for r in holder.values() if r.assignment_rule == pool]
		unworked |= routing.untouched_pairs(pool, list({r.allocated_to for r in rows}), [r.reference_name for r in rows])
	by_rep = defaultdict(list)
	for journey in due:
		h = holder.get(journey.subject_name)
		if h and (h.allocated_to, journey.subject_name) in unworked:
			by_rep[h.allocated_to].append(journey)
	leads = _leads(list(by_rep), api.MOVING_LEAD_FIELD, api.MOVING_DEFAULT_MINUTES) if by_rep else {}
	for rep, journeys in by_rep.items():
		soon = [j for j in journeys if j.reassign_at <= add_to_date(now, minutes=leads[rep])]
		if soon:
			_tell_moving(rep, soon)


def _tell_moving(rep, journeys):
	"""One notice for the rep's leads about to move, then each journey's stamp, in one transaction per rep."""
	from tatva_connect.workflow_engine import interpreter

	one = journeys[0].subject_name if len(journeys) == 1 else None
	subject = frappe.db.get_value("CRM Lead", one, "lead_name") or one if one else _("{0} leads").format(len(journeys))
	try:
		told = dispatch.notify(
			LEAD_MOVING_SOON, [rep], actor="Administrator", text=_("Moves soon unless you work it: {0}").format(subject),
			source=("CRM Lead", one) if one else _LEAD_LIST, target=("CRM Lead", one) if one else _LEAD_LIST,
			link=None if one else frappe.utils.get_url(_lead_list_route([j.subject_name for j in journeys])),
		)
		for journey in journeys if told else ():
			frappe.db.set_value(interpreter.JOURNEY_DT, journey.name, "moving_soon_told_for", journey.reassign_at, update_modified=False)  # authz-ok: tier-a — scheduler, the warning's own stamp
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Notifications: lead moving soon", message=f"rep={rep} leads={len(journeys)}\n{frappe.get_traceback()}")


def _lead_list_route(leads) -> str:
	"""The Leads list drilled (`?filters=`) to these leads."""
	return "/crm/leads/view/list?filters=" + quote(frappe.as_json({"name": ["in", leads]}, indent=None, separators=(",", ":")))


def _remind(bell_type):
	"""Due soon reads ahead by the longest lead any rep may pick, then keeps each rep's own window; overdue reads back to the floor."""
	now = now_datetime()
	soon = bell_type == TASK_DUE_SOON
	if soon:
		before, after = add_to_date(now, minutes=max(filter(None, map(api.minutes, api.lead_options())), default=api.LEAD_DEFAULT_MINUTES)), now
	else:
		before, after = now, add_to_date(now, minutes=-_OVERDUE_FLOOR_MINUTES)
	by_rep = defaultdict(list)
	for task in _pending(bell_type, before, after):
		by_rep[task.assigned_to].append(task)
	leads = _leads(list(by_rep)) if soon and by_rep else {}
	for rep, tasks in by_rep.items():
		until = add_to_date(now, minutes=leads[rep]) if soon else before
		due = [t for t in tasks if t.due_date <= until]
		if due:
			_tell(bell_type, rep, due, _task_list_route(bell_type, rep, until, after))


def _task_list_route(bell_type, rep, before, after) -> str:
	"""The Tasks list drilled (`?filters=`) to the rep's open tasks in this reminder's window, live when opened."""
	due = ["between", [after, before]] if bell_type == TASK_DUE_SOON else ["<=", before]
	filters = {"assigned_to": rep, "status": ["in", open_statuses()], "due_date": due}
	return "/crm/tasks/view/list?filters=" + quote(frappe.as_json(filters, indent=None, separators=(",", ":")))


def _tell(bell_type, rep, tasks, list_route):
	"""One notice for the rep's tasks, then their stamps, in one transaction per rep so one bad rep never stops the run."""
	stamp_field, phrase = _REMINDERS[bell_type]
	subject = (tasks[0].title or tasks[0].name) if len(tasks) == 1 else _("{0} tasks").format(len(tasks))
	one = tasks[0] if len(tasks) == 1 and tasks[0].reference_docname else None  # a lone task opens its record; anything else opens the drilled list
	try:
		told = dispatch.notify(
			bell_type, [rep], actor="Administrator", text=_(phrase).format(subject),
			source=("CRM Task", one.name) if one else _TASK_LIST,
			target=(one.reference_doctype, one.reference_docname) if one else _TASK_LIST,
			link=None if one else frappe.utils.get_url(list_route),
		)
		for task in tasks if told else ():
			frappe.db.set_value("CRM Task", task.name, stamp_field, task.due_date, update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Notifications: task reminder", message=f"{bell_type} rep={rep} tasks={len(tasks)}\n{frappe.get_traceback()}")


def reset_reminder_stamps(doc, method=None):
	"""CRM Task validate: a task handed to a new assignee is theirs to be told about, so the old assignee's stamps clear."""
	if not doc.is_new() and doc.has_value_changed("assigned_to"):
		doc.update(dict.fromkeys(REMINDER_STAMPS))


def _pending(bell_type, before, after):
	"""Open assigned tasks due inside the window whose stamp does not already name that due date, so a rescheduled task is told again; oldest first."""
	Task = frappe.qb.DocType("CRM Task")
	stamp = Task[_REMINDERS[bell_type][0]]
	window = [["due_date", "<=", before], ["due_date", ">", after]]
	return (
		frappe.qb.get_query(
			"CRM Task",
			fields=["name", "title", "assigned_to", "due_date", "reference_doctype", "reference_docname"],
			filters=[["status", "in", open_statuses()], ["assigned_to", "is", "set"], *window],
		)
		.where(stamp.isnull() | (stamp != Task.due_date))
		.orderby(Task.due_date)
		.limit(_RUN_CAP)
		.run(as_dict=True)
	)
