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

Firing once is the doc event's own job: `after_insert` runs once per row, and `has_value_changed`
is true only on the save that changed the field. The sweep has no such guarantee — it re-reads the
same task every 5 minutes — so it stamps the due date it notified for and skips a task already
stamped for that date. A rescheduled task carries a new due date, so it is warned again.
"""
import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect.notifications import dispatch

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

# A task in one of these is finished; its due date no longer concerns anyone.
_CLOSED_TASK_STATUS = ("Done", "Cancelled")


def _route(doctype, name) -> str:
	if doctype == "CRM Lead" and name:
		return f"/crm/leads/{name}"
	if doctype == "CRM Deal" and name:
		return f"/crm/deals/{name}"
	return "/crm"


def _route_for_task(doc) -> str:
	return _route(doc.get("reference_doctype"), doc.get("reference_docname"))


def _assignees(doctype, name) -> list:
	"""The users a document is assigned to — Frappe's own `_assign`, the same list crm reads."""
	if not doctype or not name:
		return []
	return frappe.parse_json(frappe.db.get_value(doctype, name, "_assign") or "[]")


def _lead_of_message(doc):
	"""The lead a WhatsApp message hangs off (a Deal answers through its originating lead)."""
	if doc.get("reference_doctype") == "CRM Lead":
		return doc.get("reference_name")
	return None


def _text(html: str) -> str:
	"""The tray renders `notification_text` as HTML, in crm's own markup."""
	return f'<div class="mb-2 leading-5 text-ink-gray-5">{html}</div>'


# ---------------------------------------------------------------------------
# Doc events — each fires exactly once, by construction.
# ---------------------------------------------------------------------------
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


def on_whatsapp_received(doc, method=None):
	"""A patient replied. crm writes the tray row itself (crm.api.whatsapp), so this adds only the live channel."""
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


def on_call_missed(doc, method=None):
	"""An inbound call nobody answered. Only the save that MOVES the status to No Answer notifies —
	a later save of the same row (a recording URL landing, say) changes nothing and tells no one."""
	if doc.get("type") != "Incoming" or doc.get("status") != "No Answer":
		return
	if not doc.has_value_changed("status"):
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


def on_lead_stage_changed(doc, method=None):
	"""The stage moved. `has_value_changed` is true only on the save that moved it, and a rep who moved
	their own lead is skipped by crm's writer (a rep is never told about their own action)."""
	if doc.is_new() or not doc.has_value_changed("custom_substage"):
		return
	stage = doc.get("custom_substage")
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


# ---------------------------------------------------------------------------
# Schedule — nothing fires when a due date simply passes, so it is swept.
# ---------------------------------------------------------------------------
def _lead_minutes() -> int:
	return _LEAD_MINUTES.get(frappe.db.get_single_value(SETTINGS, "due_soon_lead"), _LEAD_DEFAULT)


def _notify_due(task, event_key, stamp_field, title, phrase):
	"""Tell the assignee once for this due date, then stamp the date so the next sweep passes it by."""
	dispatch.notify(
		event_key,
		[task.assigned_to],
		title=title,
		body=task.title or "You have a task",
		data={"doctype": "CRM Task", "name": task.name, "route": _route(task.reference_doctype, task.reference_docname)},
		bell={
			"actor": "Administrator",
			"text": _text(f"<span>{phrase}</span> <span class='font-medium text-ink-gray-9'>{frappe.utils.escape_html(task.title or task.name)}</span>"),
			"source": ("CRM Task", task.name),
			"target": (task.reference_doctype or "CRM Lead", task.reference_docname),
		},
	)
	frappe.db.set_value("CRM Task", task.name, stamp_field, task.due_date, update_modified=False)


def sweep_task_due():
	"""Every 5 minutes: warn about a task about to fall due, and tell a rep about one that already has.

	Both switches are read per pass, so either can be off without the other paying for it. A task is
	stamped with the due date it was told about — the next sweep sees the stamp and passes it by, and a
	rescheduled task carries a new due date, so it is told again. Nothing here notifies twice.
	"""
	from tatva_connect import automation

	due_soon = automation.is_enabled("Notify::Task::due-soon")
	overdue = automation.is_enabled("Notify::Task::overdue")
	if not (due_soon or overdue):
		return

	now = now_datetime()

	if due_soon:
		horizon = add_to_date(now, minutes=_lead_minutes())
		for task in _pending("custom_due_soon_notified_for", after=now, before=horizon):
			_notify_due(task, "Task::Due::soon", "custom_due_soon_notified_for", "Task due soon", "Due soon:")

	if overdue:
		for task in _pending("custom_overdue_notified_for", before=now):
			_notify_due(task, "Task::Due::overdue", "custom_overdue_notified_for", "Task overdue", "Overdue:")


def _pending(stamp_field, before, after=None):
	"""Open, assigned tasks whose due date falls in the window and whose stamp does not already name
	that due date. The stamp is compared to `due_date` COLUMN to COLUMN — a task told about at 5pm and
	then rescheduled to 6pm no longer matches its stamp, so it is told again; an untouched one never is.
	`frappe.get_all` filters cannot compare two columns, so this is the query builder."""
	Task = frappe.qb.DocType("CRM Task")
	stamp = Task[stamp_field]
	q = (
		frappe.qb.from_(Task)
		.select(Task.name, Task.title, Task.assigned_to, Task.due_date, Task.reference_doctype, Task.reference_docname)
		.where(Task.status.notin(_CLOSED_TASK_STATUS))
		.where(Task.assigned_to.isnotnull() & (Task.assigned_to != ""))
		.where(Task.due_date.isnotnull())
		.where(Task.due_date < before)
		.where(stamp.isnull() | (stamp != Task.due_date))
	)
	if after is not None:
		q = q.where(Task.due_date > after)
	return q.run(as_dict=True)
