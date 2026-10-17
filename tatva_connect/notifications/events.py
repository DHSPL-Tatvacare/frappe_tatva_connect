"""Notification triggers: each hook asks `dispatch.armed` before its @fail_safe body opens a savepoint, then resolves WHO + WHAT for `dispatch.notify`; task reminders are a 5-minute cron, one notice per rep."""
from collections import defaultdict
from urllib.parse import quote

import frappe
from crm.api.doc import get_assigned_users
from frappe.utils import add_to_date, cint, now_datetime

from tatva_connect.notifications import catalog, deep_link, dispatch, prefs
from tatva_connect.notifications.catalog import (
	CALL_MISSED,
	DUE_SOON,
	LEAD_ASSIGNED,
	OVERDUE,
	WHATSAPP_RECEIVED,
)
from tatva_connect.propagate import fail_safe
from tatva_connect.tasks.tasks import open_statuses

# One run reads at most this many tasks, oldest first; the rest are told by the next run.
_RUN_CAP = 5000

# The settings' options read as `N unit(s)`; blank falls back to these, and a wordy option (`Every overdue task`) means no limit.
_MINUTES_PER = {"minute": 1, "hour": 60, "day": 1440}
_LEAD_DEFAULT_MINUTES = 60
_FLOOR_DEFAULT_MINUTES = 7 * 1440


def _minutes(option):
	"""Minutes an `N unit(s)` option spells (`1 hour` -> 60); None for any other wording."""
	number, _, unit = (option or "").partition(" ")
	per = _MINUTES_PER.get(unit.rstrip("s"))
	return cint(number) * per if number.isdigit() and per else None


def _route(doctype, name) -> str:
	"""The app route of a record, from deep_link's one map; `/crm` when it has none."""
	route = deep_link.ROUTES.get(doctype) if name else None
	return route.format(name=name) if route else "/crm"


def _assignees(doctype, name) -> list:
	"""Who is told: crm's own resolver, falling back to `lead_owner` for an unassigned lead, as automation/actions.py does."""
	if not doctype or not name:
		return []
	owner = frappe.db.get_value("CRM Lead", name, "lead_owner") if doctype == "CRM Lead" else None
	return list(get_assigned_users(doctype, name, default_assigned_to=owner) or [])


def _lead_of_message(doc):
	"""The lead a WhatsApp message hangs off; a Deal answers through its originating lead, as whatsapp/routing.py does."""
	dt, dn = doc.get("reference_doctype"), doc.get("reference_name")
	if dt == "CRM Lead":
		return dn
	if dt == "CRM Deal" and dn:
		return frappe.db.get_value("CRM Deal", dn, "lead")
	return None


# Doc events fire once; @fail_safe keeps a refused alert from taking the save (task-assigned/stage-changed archived 2026-10-02).
def on_lead_assigned(doc, method=None):
	"""ToDo after_insert: only a CRM Lead assignment."""
	if doc.reference_type == "CRM Lead" and doc.allocated_to and dispatch.armed(LEAD_ASSIGNED):
		_lead_assigned(doc)


@fail_safe
def _lead_assigned(doc, method=None):
	lead_name = frappe.db.get_value("CRM Lead", doc.reference_name, "lead_name") or doc.reference_name
	dispatch.notify(
		LEAD_ASSIGNED,
		[doc.allocated_to],
		title="New lead assigned",
		body=lead_name,
		data={"doctype": "CRM Lead", "name": doc.reference_name, "route": _route("CRM Lead", doc.reference_name)},
	)


def on_whatsapp_received(doc, method=None):
	"""WhatsApp Message after_insert: a live inbound reply, never one backfilled or recovered (`in_workflow`, whatsapp.ingest.apply_historical)."""
	if doc.get("type") == "Incoming" and not frappe.flags.get("in_workflow") and dispatch.armed(WHATSAPP_RECEIVED):
		_whatsapp_received(doc)


@fail_safe
def _whatsapp_received(doc, method=None):
	"""crm writes the tray row itself (crm.api.whatsapp), so this adds only the live channel."""
	lead = _lead_of_message(doc)
	if not lead:
		return
	dispatch.notify(
		WHATSAPP_RECEIVED,
		_assignees("CRM Lead", lead),
		title="Lead replied on WhatsApp",
		body=(doc.get("message") or "")[:120] or "New WhatsApp message",
		data={"doctype": "CRM Lead", "name": lead, "route": _route("CRM Lead", lead)},
	)


def on_call_missed(doc, method=None):
	"""CRM Call Log on_update: only the save that MOVES an inbound call to No Answer, never a later save of it (`get_doc_before_save`, not `has_value_changed`)."""
	if doc.get("type") != "Incoming" or doc.get("status") != "No Answer":
		return
	before = doc.get_doc_before_save()
	if before and before.get("status") != doc.get("status") and dispatch.armed(CALL_MISSED):
		_call_missed(doc)


@fail_safe
def _call_missed(doc, method=None):
	lead = doc.get("reference_docname") if doc.get("reference_doctype") == "CRM Lead" else None
	if not lead:
		return
	caller = doc.get("from") or "a lead"
	dispatch.notify(
		CALL_MISSED,
		_assignees("CRM Lead", lead),
		title="Missed call",
		body=f"{caller} called and did not get through",
		data={"doctype": "CRM Lead", "name": lead, "route": _route("CRM Lead", lead)},
		bell={
			"actor": doc.owner,
			"text": f"<span>Missed call from</span> <span class='font-medium text-ink-gray-9'>{frappe.utils.escape_html(caller)}</span>",
			"source": ("CRM Call Log", doc.name),
			"target": ("CRM Lead", lead),
		},
	)


# Schedule: a due date passing fires nothing, so a 5-minute cron tells each rep once per reminder type, however many tasks they have.
_REMINDERS = {
	DUE_SOON: ("custom_due_soon_notified_for", "Task due soon", "Due soon:"),
	OVERDUE: ("custom_overdue_notified_for", "Task overdue", "Overdue:"),
}
REMINDER_STAMPS = tuple(stamp for stamp, _, _ in _REMINDERS.values())
_TASK_LIST = ("CRM Task", None)


def _lead_minutes() -> int:
	return _minutes(frappe.db.get_single_value(catalog.ORG_SETTINGS, "due_soon_lead")) or _LEAD_DEFAULT_MINUTES


def _overdue_floor(now):
	"""The oldest overdue task still worth telling, so arming the switch never announces the whole backlog; None = no limit."""
	option = frappe.db.get_single_value(catalog.ORG_SETTINGS, "overdue_floor")
	minutes = _minutes(option) if option else _FLOOR_DEFAULT_MINUTES
	return None if minutes is None else add_to_date(now, minutes=-minutes)


def _window(event_key, now):
	"""(before, after) on `due_date` for the reminders whose moment has come: due-soon looks ahead by the lead time, overdue back to the floor."""
	if event_key == DUE_SOON:
		return add_to_date(now, minutes=_lead_minutes()), now
	return now, _overdue_floor(now)


def sweep_reminders():
	"""Every 5 minutes: each armed reminder, one notice per rep."""
	for event_key in _REMINDERS:
		if dispatch.armed(event_key):
			_remind(event_key)


def _remind(event_key):
	subscribed = prefs.subscriber_users(event_key)
	if not subscribed:
		return
	by_rep = defaultdict(list)
	before, after = _window(event_key, now_datetime())
	for task in _pending(event_key, subscribed, before, after):
		by_rep[task.assigned_to].append(task)
	for rep, tasks in by_rep.items():
		_tell(event_key, rep, tasks, _task_list_route(event_key, rep, before, after))


def _task_list_route(event_key, rep, before, after) -> str:
	"""The Tasks list drilled (`?filters=`) to the rep's open tasks in this reminder's window, live when opened."""
	due = ["between", [after, before]] if event_key == DUE_SOON else ["<=", before]
	filters = {"assigned_to": rep, "status": ["in", open_statuses()], "due_date": due}
	return "/crm/tasks/view/list?filters=" + quote(frappe.as_json(filters, indent=None, separators=(",", ":")))


def _tell(event_key, rep, tasks, list_route):
	"""One notice for the rep's tasks, then their stamps, in one transaction per rep so one bad rep never stops the run."""
	stamp_field, title, phrase = _REMINDERS[event_key]
	one = tasks[0] if len(tasks) == 1 else None
	subject = (one.title or one.name) if one else f"{len(tasks)} tasks"
	target = (one.reference_doctype, one.reference_docname) if one else _TASK_LIST
	bell = None
	if not one or (one.reference_doctype and one.reference_docname):
		bell = {
			"actor": "Administrator",
			"text": f"<span>{phrase}</span> <span class='font-medium text-ink-gray-9'>{frappe.utils.escape_html(subject)}</span>",
			"source": ("CRM Task", one.name) if one else _TASK_LIST,
			"target": target,
			"link": None if one else frappe.utils.get_url(list_route),
		}
	try:
		told = dispatch.notify(
			event_key, [rep], title=title, body=subject, bell=bell,
			data={"doctype": "CRM Task", "name": one.name if one else "", "route": _route(*target) if one else list_route},
		)
		for task in tasks if told else ():
			frappe.db.set_value("CRM Task", task.name, stamp_field, task.due_date, update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Notifications: task reminder", message=f"{event_key} rep={rep} tasks={len(tasks)}\n{frappe.get_traceback()}")


def reset_reminder_stamps(doc, method=None):
	"""CRM Task validate: a task handed to a new assignee is theirs to be told about, so the old assignee's stamps clear."""
	if not doc.is_new() and doc.has_value_changed("assigned_to"):
		doc.update(dict.fromkeys(REMINDER_STAMPS))


def _pending(event_key, subscribed, before, after=None):
	"""Open tasks of opted-in reps due inside the window whose stamp does not already name that due date, so a rescheduled task is told again; oldest first."""
	Task = frappe.qb.DocType("CRM Task")
	stamp = Task[_REMINDERS[event_key][0]]
	window = [["due_date", "<=", before]] + ([["due_date", ">", after]] if after is not None else [])
	return (
		frappe.qb.get_query(
			"CRM Task",
			fields=["name", "title", "assigned_to", "due_date", "reference_doctype", "reference_docname"],
			filters=[["status", "in", open_statuses()], ["assigned_to", "in", subscribed], *window],
		)
		.where(stamp.isnull() | (stamp != Task.due_date))
		.orderby(Task.due_date)
		.limit(_RUN_CAP)
		.run(as_dict=True)
	)
