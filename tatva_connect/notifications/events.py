"""Notification triggers: each hook asks `dispatch.armed` before its @fail_safe body opens a savepoint, then resolves WHO + WHAT for `dispatch.notify`; task reminders are passes parked at their moment on the workflow lane."""
import time

import frappe
from crm.api.doc import get_assigned_users
from frappe.utils import add_to_date, get_datetime, now_datetime

from tatva_connect.notifications import catalog, dispatch, prefs
from tatva_connect.notifications.catalog import (
	CALL_MISSED,
	DUE_SOON,
	LEAD_ASSIGNED,
	OVERDUE,
	WHATSAPP_RECEIVED,
)
from tatva_connect.propagate import fail_safe
from tatva_connect.tasks.tasks import CLOSED_STATUSES, open_statuses
from tatva_connect.utils import book_drain, hold_drain, lane_has_room, next_clock_at, release_drain

# The operator's lead time (CRM Notification Settings) -> minutes. A blank/unknown value reads as the field default.
_LEAD_MINUTES = {
	"5 minutes": 5,
	"15 minutes": 15,
	"30 minutes": 30,
	"1 hour": 60,
	"3 hours": 180,
	"24 hours": 1440,
}
_LEAD_DEFAULT = 60

# One pass never tells more than this; the rest are told by the pass it parks next (oldest first, so a pass always makes progress).
_SWEEP_CAP = 200

# The operator's overdue floor (CRM Notification Settings) -> days back. None = every overdue task, however old.
_FLOOR_DAYS = {
	"1 day": 1,
	"3 days": 3,
	"7 days": 7,
	"30 days": 30,
	"Every overdue task": None,
}
_FLOOR_DEFAULT_DAYS = 7


def _route(doctype, name) -> str:
	if doctype == "CRM Lead" and name:
		return f"/crm/leads/{name}"
	if doctype == "CRM Deal" and name:
		return f"/crm/deals/{name}"
	return "/crm"


def _assignees(doctype, name) -> list:
	"""Who is told about this document — crm's OWN resolver, so the bell row crm writes and the push we
	send always reach the same people. A lead with an owner but no assignment (an LSQ or partner-API
	import) falls back to `lead_owner` through crm's own `default_assigned_to`, the rule the automation
	plane already applies (automation/actions.py)."""
	if not doctype or not name:
		return []
	owner = frappe.db.get_value("CRM Lead", name, "lead_owner") if doctype == "CRM Lead" else None
	return list(get_assigned_users(doctype, name, default_assigned_to=owner) or [])


def _lead_of_message(doc):
	"""The lead a WhatsApp message hangs off. A Deal answers through its originating lead — the hop the
	WhatsApp router already makes (whatsapp/routing.py) — so a reply on a Deal reaches the same reps crm
	bells for it, rather than nobody."""
	dt, dn = doc.get("reference_doctype"), doc.get("reference_name")
	if dt == "CRM Lead":
		return dn
	if dt == "CRM Deal" and dn:
		return frappe.db.get_value("CRM Deal", dn, "lead")
	return None


# Doc events — each fires exactly once, by construction. PROPAGATE: an alert is not the work, so a
# transport that refuses must never take the rep's save with it (@fail_safe, tatva_connect/propagate.py).
# Task-assigned and stage-changed notifications archived in .archive/notify-task-assigned-stage-changed-2026-10-02: lead assigned and the task reminders cover a rep's work.
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
		data={"doctype": "CRM Lead", "name": doc.reference_name, "route": f"/crm/leads/{doc.reference_name}"},
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


# Schedule — nothing fires when a due date simply passes, so each reminder's pass is PARKED at its next moment on the workflow lane and woken there (workflow_engine.wakeups); a */15 backstop covers a lost booking.
_REMINDERS = {
	DUE_SOON: ("custom_due_soon_notified_for", "Task due soon", "Due soon:"),
	OVERDUE: ("custom_overdue_notified_for", "Task overdue", "Overdue:"),
}
REMINDER_STAMPS = tuple(stamp for stamp, _, _ in _REMINDERS.values())
_PARK_ON = ("due_date", "assigned_to", "status")
_PASS = "tatva_connect.notifications.events.run_reminders"


def _lead_minutes() -> int:
	return _LEAD_MINUTES.get(frappe.db.get_single_value(catalog.ORG_SETTINGS, "due_soon_lead"), _LEAD_DEFAULT)


def _overdue_floor(now):
	"""The oldest overdue task worth telling a rep about. Without a floor, the first pass after the switch
	is armed announces the site's entire historical backlog; the operator says how far back is still news."""
	days = _FLOOR_DAYS.get(frappe.db.get_single_value(catalog.ORG_SETTINGS, "overdue_floor"), _FLOOR_DEFAULT_DAYS)
	return None if days is None else add_to_date(now, days=-days)


def _lead(event_key) -> int:
	"""Minutes before the due date this reminder fires: the operator's lead time for due-soon, none for overdue."""
	return _lead_minutes() if event_key == DUE_SOON else 0


def _window(event_key, now):
	"""(before, after) on `due_date` for the reminders whose moment has come: due-soon looks ahead by the lead time, overdue back to the floor."""
	return add_to_date(now, minutes=_lead(event_key)), (now if event_key == DUE_SOON else _overdue_floor(now))


def _lane_key(event_key) -> str:
	return f"notify:{event_key}"


def _book(event_key, at):
	"""Park the next pass at `at`, never pushing an earlier booking back (the drain's `pull_forward`)."""
	from tatva_connect.workflow_engine import wakeups

	wakeups.schedule_on_lane(at, _PASS, {"event_key": event_key}, _lane_key(event_key), only_if_earlier=True)


def _kick(event_key):
	"""A moment already come: one pass now, deduplicated against one already queued (the drain's `kick`)."""
	from tatva_connect.workflow_engine import wakeups

	frappe.enqueue(
		_PASS, queue=wakeups.WAKE_QUEUE, job_id=_lane_key(event_key), deduplicate=True, enqueue_after_commit=True,
		now=bool(frappe.flags.get("in_test")), event_key=event_key,
	)


def _notify_due(task, event_key) -> bool:
	"""Tell the assignee, and stamp the due date ONLY if they were actually told. A task with no lead or
	deal behind it gets no tray row — a row whose click routes nowhere is worse than no row."""
	stamp_field, title, phrase = _REMINDERS[event_key]
	bell = None
	if task.reference_doctype and task.reference_docname:
		bell = {
			"actor": "Administrator",
			"text": f"<span>{phrase}</span> <span class='font-medium text-ink-gray-9'>{frappe.utils.escape_html(task.title or task.name)}</span>",
			"source": ("CRM Task", task.name),
			"target": (task.reference_doctype, task.reference_docname),
		}
	told = dispatch.notify(
		event_key,
		[task.assigned_to],
		title=title,
		body=task.title or "You have a task",
		data={"doctype": "CRM Task", "name": task.name, "route": _route(task.reference_doctype, task.reference_docname)},
		bell=bell,
	)
	if not told:
		return False
	frappe.db.set_value("CRM Task", task.name, stamp_field, task.due_date, update_modified=False)
	return True


def sweep_reminders():
	"""*/15 backstop for every reminder pass, so a lost booking costs at most one sweep."""
	for event_key in _REMINDERS:
		run_reminders(event_key)


def reset_reminder_stamps(doc, method=None):
	"""CRM Task validate: a task handed to a new assignee is theirs to be told about, so the old assignee's stamps clear."""
	if not doc.is_new() and doc.has_value_changed("assigned_to"):
		doc.update(dict.fromkeys(REMINDER_STAMPS))


def park_reminders(doc, method=None):
	"""CRM Task on_update: a new due date, assignee or status may hold the earliest reminder ahead."""
	if not doc.get("due_date") or not doc.get("assigned_to") or doc.get("status") in CLOSED_STATUSES:
		return
	if any(doc.has_value_changed(f) for f in _PARK_ON) and any(dispatch.armed(k) for k in _REMINDERS):
		_park(doc)


@fail_safe
def _park(doc, method=None):
	for event_key in _REMINDERS:
		if dispatch.armed(event_key) and prefs.subscribers(event_key, [doc.assigned_to]):
			moment = add_to_date(get_datetime(doc.due_date), minutes=-_lead(event_key))
			if moment <= now_datetime():
				_kick(event_key)
			else:
				_book(event_key, moment)


def run_reminders(event_key):
	"""One pass the drain's way (workflow_engine/drain.run): a renewed lease, a slice of one pace interval, and room on `short` before each send."""
	from tatva_connect.workflow_engine import thresholds

	if not dispatch.armed(event_key):
		return
	key, interval = _lane_key(event_key), thresholds.DRAIN_INTERVAL_SECONDS
	lease = interval * thresholds.DRAIN_LEASE_MULTIPLE
	started = now_datetime()
	if not book_drain(key, lease):
		_book(event_key, add_to_date(started, seconds=interval))  # the holder may finish before this moment is due
		frappe.db.commit()
		return
	subscribed = prefs.subscriber_users(event_key)
	before, after = _window(event_key, started)
	until = time.monotonic() + interval
	tasks, tried, told = [], 0, 0
	try:
		if subscribed:
			tasks = _pending(event_key, subscribed, before, after)
		for task in tasks[:_SWEEP_CAP]:
			if time.monotonic() >= until or not lane_has_room("short"):
				break
			hold_drain(key, lease)
			told += _tell(event_key, task)
			tried += 1
	finally:
		release_drain(key)
	if tried < len(tasks):
		_book(event_key, now_datetime() if told else add_to_date(started, seconds=interval))  # no progress waits a pace interval, never a tight loop
	elif subscribed:
		ahead = _next_due(subscribed, after=before)
		if ahead:
			_book(event_key, add_to_date(ahead, minutes=-_lead(event_key)))
	frappe.db.commit()


def _tell(event_key, task) -> int:
	"""Tell one rep and stamp the task in its own transaction, so no row lock outlives its task and one bad row never stops the pass."""
	try:
		_notify_due(task, event_key)
		frappe.db.commit()
		return 1
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Notifications: task reminder", message=f"{event_key} task={task.name}\n{frappe.get_traceback()}")
		return 0


def _candidates(subscribed, statuses) -> list:
	"""THE reminder population: open tasks of opted-in users — the one filter both reads build on."""
	return [["status", "in", statuses], ["assigned_to", "in", subscribed]]


def _next_due(subscribed, after):
	"""The earliest due date ahead, asked once per open status so each read walks (status, due_date) in order and stops at its first hit."""
	return min(filter(None, (next_clock_at("CRM Task", "due_date", _candidates(subscribed, [s]), after=after) for s in open_statuses())), default=None)


def _pending(event_key, subscribed, before, after=None):
	"""Candidates due inside the window whose stamp does not already name that due date — compared COLUMN to COLUMN, so a rescheduled task is told again. Oldest first, so a capped pass always makes progress."""
	Task = frappe.qb.DocType("CRM Task")
	stamp = Task[_REMINDERS[event_key][0]]
	window = [["due_date", "<=", before]] + ([["due_date", ">", after]] if after is not None else [])
	return (
		frappe.qb.get_query(
			"CRM Task",
			fields=["name", "title", "assigned_to", "due_date", "reference_doctype", "reference_docname"],
			filters=_candidates(subscribed, open_statuses()) + window,
		)
		.where(stamp.isnull() | (stamp != Task.due_date))
		.orderby(Task.due_date)
		.limit(_SWEEP_CAP + 1)
		.run(as_dict=True)
	)


def assert_reminder_lane():
	"""after_migrate: an armed reminder parks its passes on the workflow lane, so that lane must be registered."""
	from tatva_connect.workflow_engine import thresholds, wakeups

	if any(dispatch.armed(k) for k in _REMINDERS):
		wakeups.assert_lane(
			wakeups.WAKE_QUEUE, thresholds.WAKE_JOB_TIMEOUT, catalog.MASTER,
			f"Register it, then run `{wakeups.LANE_WORKER_COMMAND}` and `{wakeups.LANE_SCHEDULER_COMMAND}`.",
		)
