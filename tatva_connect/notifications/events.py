"""Notification triggers — resolve the recipient + hand off to the ONE send path.

  * New lead assigned to a rep   -> ToDo.after_insert (reference_type == CRM Lead)
  * New task assigned to a rep   -> CRM Task.after_insert
  * Patient replied on WhatsApp  -> WhatsApp Message.after_insert (Incoming)
  * Missed call from a patient   -> CRM Call Log.on_update (status -> No Answer, inbound)
  * Lead stage moved             -> CRM Lead.on_update (custom_substage changed)
  * Task due soon / overdue      -> the 5-minute sweep below (no doc event fires for time passing)

Each handler does the minimum: figure out WHO + WHAT, then call `dispatch.notify`. The global
gate, the opt-in filter, the bell row, presence routing and FCM transport all live behind
`dispatch.notify` — none of that leaks back here (one brain, invariant A.8).

Firing once is the doc event's own job: `after_insert` runs once per row, and a "it moved" handler asks
`get_doc_before_save()` — NOT `has_value_changed`, which returns True for every field when there is no
previous version (`document.py:684`), so a lead ARRIVING at a stage read as a lead that moved to it. The
sweep has no such guarantee — it re-reads the
same task every 5 minutes — so it stamps the due date it TOLD a rep about, and skips a task already
stamped for that date. A rescheduled task carries a new due date, so it is told again. The sweep only
ever selects tasks whose rep has opted in, so a task nobody can be told about is never selected, never
capped and never stamped — it is told the day its rep opts in.
"""
import frappe
from crm.api.doc import get_assigned_users
from frappe.utils import add_to_date, now_datetime

from tatva_connect.notifications import dispatch, prefs
from tatva_connect.propagate import fail_safe
from tatva_connect.tasks.tasks import CLOSED_STATUSES
from tatva_connect.taxonomy import labels

SETTINGS = "CRM Notification Settings"

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

# One pass never tells more than this; the rest are told five minutes later (oldest first, so a pass always makes progress).
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


def _route_for_task(doc) -> str:
	return _route(doc.get("reference_doctype"), doc.get("reference_docname"))


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


def _text(html: str) -> str:
	"""The tray renders `notification_text` as HTML, in crm's own markup."""
	return f'<div class="mb-2 leading-5 text-ink-gray-5">{html}</div>'


# Doc events — each fires exactly once, by construction. PROPAGATE: an alert is not the work, so a
# transport that refuses must never take the rep's save with it (@fail_safe, tatva_connect/propagate.py).
@fail_safe
def on_task_created(doc, method=None):
	if not doc.get("assigned_to"):
		return
	dispatch.notify(
		"Task::Assignment::assigned",
		[doc.assigned_to],
		title="New task assigned",
		body=doc.get("title") or "You have a new task",
		data={"doctype": "CRM Task", "name": doc.name, "route": _route_for_task(doc)},
	)


def on_lead_assigned(doc, method=None):
	# doc is the ToDo created by assignment; act only on CRM Lead assignments.
	if doc.reference_type != "CRM Lead" or not doc.allocated_to:
		return
	lead_name = frappe.db.get_value("CRM Lead", doc.reference_name, "lead_name") or doc.reference_name
	dispatch.notify(
		"Lead::Assignment::assigned",
		[doc.allocated_to],
		title="New lead assigned",
		body=lead_name,
		data={"doctype": "CRM Lead", "name": doc.reference_name, "route": f"/crm/leads/{doc.reference_name}"},
	)


@fail_safe
def on_whatsapp_received(doc, method=None):
	"""A patient replied. crm writes the tray row itself (crm.api.whatsapp), so this adds only the live channel."""
	if frappe.flags.get("in_workflow"):
		return  # backfilled or recovered, not a live reply — see whatsapp.ingest.apply_historical
	if doc.get("type") != "Incoming":
		return
	lead = _lead_of_message(doc)
	if not lead:
		return
	dispatch.notify(
		"WhatsApp::Message::received",
		_assignees("CRM Lead", lead),
		title="Patient replied on WhatsApp",
		body=(doc.get("message") or "")[:120] or "New WhatsApp message",
		data={"doctype": "CRM Lead", "name": lead, "route": _route("CRM Lead", lead)},
	)


@fail_safe
def on_call_missed(doc, method=None):
	"""An inbound call nobody answered. Only the save that MOVES the status to No Answer notifies —
	a later save of the same row (a recording URL landing, say) changes nothing and tells no one."""
	if doc.get("type") != "Incoming" or doc.get("status") != "No Answer":
		return
	before = doc.get_doc_before_save()
	if not before or before.get("status") == doc.get("status"):
		return
	lead = doc.get("reference_docname") if doc.get("reference_doctype") == "CRM Lead" else None
	if not lead:
		return
	caller = doc.get("from") or "a patient"
	dispatch.notify(
		"Telephony::Call::missed",
		_assignees("CRM Lead", lead),
		title="Missed call",
		body=f"{caller} called and did not get through",
		data={"doctype": "CRM Lead", "name": lead, "route": _route("CRM Lead", lead)},
		bell={
			"actor": doc.owner,
			"text": _text(f"<span>Missed call from</span> <span class='font-medium text-ink-gray-9'>{frappe.utils.escape_html(caller)}</span>"),
			"source": ("CRM Call Log", doc.name),
			"target": ("CRM Lead", lead),
		},
	)


@fail_safe
def on_lead_stage_changed(doc, method=None):
	"""The stage MOVED — a lead that arrives already at a stage has not moved to it.

	A rep who moved their own lead is skipped by crm's writer (a rep is never told about their own action).
	"""
	before = doc.get_doc_before_save()
	if not before or before.get("custom_substage") == doc.get("custom_substage"):
		return
	# The stored value is the composite key; this text lands on a rep's lock screen, so name the stage.
	stage = labels.label(doc.get("custom_substage"), labels.LEAD_STAGE)
	if not stage:
		return
	dispatch.notify(
		"Lead::Stage::changed",
		_assignees("CRM Lead", doc.name),
		title="Lead stage changed",
		body=f"{doc.get('lead_name') or doc.name} moved to {stage}",
		data={"doctype": "CRM Lead", "name": doc.name, "route": _route("CRM Lead", doc.name)},
		bell={
			"actor": frappe.session.user,
			"text": _text(f"<span>moved</span> <span class='font-medium text-ink-gray-9'>{frappe.utils.escape_html(doc.get('lead_name') or doc.name)}</span> <span>to</span> <span class='font-medium text-ink-gray-9'>{frappe.utils.escape_html(stage)}</span>"),
			"source": ("CRM Lead", doc.name),
			"target": ("CRM Lead", doc.name),
		},
	)


# Schedule — nothing fires when a due date simply passes, so it is swept.
def _lead_minutes() -> int:
	return _LEAD_MINUTES.get(frappe.db.get_single_value(SETTINGS, "due_soon_lead"), _LEAD_DEFAULT)


def _overdue_floor(now):
	"""The oldest overdue task worth telling a rep about. Without a floor, the first pass after the switch
	is armed announces the site's entire historical backlog; the operator says how far back is still news."""
	days = _FLOOR_DAYS.get(frappe.db.get_single_value(SETTINGS, "overdue_floor"), _FLOOR_DEFAULT_DAYS)
	return None if days is None else add_to_date(now, days=-days)


def _notify_due(task, event_key, stamp_field, title, phrase) -> bool:
	"""Tell the assignee, and stamp the due date ONLY if they were actually told. A task with no lead or
	deal behind it gets no tray row — a row whose click routes nowhere is worse than no row."""
	bell = None
	if task.reference_doctype and task.reference_docname:
		bell = {
			"actor": "Administrator",
			"text": _text(f"<span>{phrase}</span> <span class='font-medium text-ink-gray-9'>{frappe.utils.escape_html(task.title or task.name)}</span>"),
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


# Two scheduled seams, not one function reading two switches: as one hooked path both registry rows claimed to back it, and an entry point with two owners cannot answer "is this seam armed".


def sweep_due_soon():
	"""Every 5 min: warn a rep about a task falling due inside the operator's lead time. Opted-in reps only, capped per pass."""
	from tatva_connect import automation

	if not automation.is_enabled("Notify::Task::due-soon"):
		return
	now = now_datetime()
	horizon = add_to_date(now, minutes=_lead_minutes())
	_run_pass("Task::Due::soon", "custom_due_soon_notified_for", "Task due soon", "Due soon:", before=horizon, after=now)


def sweep_overdue():
	"""Every 5 min: tell a rep about a task past its due date and not done. Floored, so arming the switch does not announce the backlog."""
	from tatva_connect import automation

	if not automation.is_enabled("Notify::Task::overdue"):
		return
	now = now_datetime()
	_run_pass("Task::Due::overdue", "custom_overdue_notified_for", "Task overdue", "Overdue:", before=now, after=_overdue_floor(now))


def _run_pass(event_key, stamp_field, title, phrase, before, after):
	subscribed = prefs.subscriber_users(event_key)
	if not subscribed:
		return
	tasks = _pending(stamp_field, subscribed, before=before, after=after)
	for task in tasks[:_SWEEP_CAP]:
		_notify_due(task, event_key, stamp_field, title, phrase)
	if len(tasks) > _SWEEP_CAP:
		frappe.logger("notifications").info(
			f"{event_key}: capped at {_SWEEP_CAP} this pass; more remain and are told by the next sweep"
		)


def _pending(stamp_field, subscribed, before, after=None):
	"""Open tasks assigned to an OPTED-IN rep whose due date falls in the window and whose stamp does not
	already name that due date. The stamp is compared to `due_date` COLUMN to COLUMN — a task told about at
	5pm and then rescheduled to 6pm no longer matches its stamp, so it is told again; an untouched one never
	is. `frappe.get_all` filters cannot compare two columns, so this is the query builder. Oldest first, so
	a capped pass always makes progress."""
	Task = frappe.qb.DocType("CRM Task")
	stamp = Task[stamp_field]
	q = (
		frappe.qb.from_(Task)
		.select(Task.name, Task.title, Task.assigned_to, Task.due_date, Task.reference_doctype, Task.reference_docname)
		.where(Task.status.notin(CLOSED_STATUSES))
		.where(Task.assigned_to.isin(subscribed))
		.where(Task.due_date.isnotnull())
		.where(Task.due_date < before)
		.where(stamp.isnull() | (stamp != Task.due_date))
		.orderby(Task.due_date)
		.limit(_SWEEP_CAP + 1)
	)
	if after is not None:
		q = q.where(Task.due_date > after)
	return q.run(as_dict=True)
