"""Notification triggers: each asks `dispatch.armed` first, then resolves WHO + WHAT for `dispatch.notify`; task reminders are a 5-minute cron, one notice per rep."""
from collections import defaultdict
from urllib.parse import quote

import frappe
from crm.api.doc import get_assigned_users
from frappe import _
from frappe.utils import add_to_date, now_datetime

from tatva_connect.notifications import api, dispatch
from tatva_connect.notifications.api import LEAD_FIELD, USER_SETTINGS
from tatva_connect.notifications.dispatch import MISSED_CALL, TASK_DUE_SOON, TASK_OVERDUE
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
_TASK_LIST = ("CRM Task", None)


def _leads(reps) -> dict:
	"""{rep: minutes} from each rep's own row, in one read; a rep with none set gets the hour."""
	rows = frappe.get_all(USER_SETTINGS, filters={"name": ["in", reps]}, fields=["name", LEAD_FIELD])
	chosen = {r.name: api.minutes(r.get(LEAD_FIELD)) for r in rows}
	return {rep: chosen.get(rep) or api.LEAD_DEFAULT_MINUTES for rep in reps}


def sweep_reminders():
	"""Every 5 minutes, while the switch is on: each reminder, one notice per rep."""
	if not dispatch.armed():
		return
	for bell_type in _REMINDERS:
		_remind(bell_type)


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
