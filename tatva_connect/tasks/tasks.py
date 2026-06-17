"""CRM Task automations: seed + enforce checklists, idempotent follow-up helper."""
import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from tatva_connect import automation

DONE_STATUS = "Done"
CLOSED_STATUSES = ("Done", "Canceled")
CALL_LEAD_TYPE = "Call Lead"


def on_lead_assignment(doc, method=None):
	"""ToDo.after_insert — when a CRM Lead is assigned to an agent, raise ONE open
	'Call Lead' task for that agent (the on-lead-create follow-up). Fires after the
	Assignment Rule sets the owner; gated by the master switch (OFF by default)."""
	if doc.reference_type != "CRM Lead" or not doc.allocated_to:
		return
	if not automation.is_enabled("Task::Assignment::followup"):
		return
	lead = frappe.db.get_value(
		"CRM Lead", doc.reference_name, ["lead_name", "custom_current_program"], as_dict=True
	) or frappe._dict()
	lead_name = lead.lead_name or doc.reference_name
	create_followup_task(
		lead=doc.reference_name,
		task_type=CALL_LEAD_TYPE,
		due_in_hours=24,
		assigned_to=doc.allocated_to,
		title=_("Call lead — {0}").format(lead_name),
	)
	# Field-sales: if the lead's program names a first activity type (config), raise ONE open
	# activity-task of it (reuses the same idempotent throttle). Dormant when the mapping is unset.
	first_type = lead.custom_current_program and frappe.db.get_value(
		"CRM Program", lead.custom_current_program, "custom_first_activity_type"
	)
	# Guard: a stale/renamed mapping must never abort the assignment ToDo insert.
	if first_type and frappe.db.exists("CRM Task Type", first_type):
		try:
			create_followup_task(
				lead=doc.reference_name,
				task_type=first_type,
				due_in_hours=48,
				assigned_to=doc.allocated_to,
				title=_("{0} — {1}").format(first_type, lead_name),
			)
		except Exception:
			frappe.log_error("on_lead_assignment: first activity-task creation failed")


def seed_checklist(doc, method=None):
	"""Fill a task's checklist from the most-specific template for the linked lead's
	(Product Line / Group / Program) + the task's type. Runs at creation, or when a
	type is first set on an existing task. No type / no matching template -> no
	checklist (task closes freely). Never overwrites a caller-supplied checklist."""
	if doc.custom_checklist or not doc.custom_task_type:
		return
	if not doc.is_new():
		before = doc.get_doc_before_save()
		if before and before.custom_task_type == doc.custom_task_type:
			return  # type unchanged on an existing task — nothing to seed

	tmpl = resolve_template(doc.custom_task_type, *_lead_axes(doc))
	if not tmpl:
		return
	for row in tmpl.items:
		doc.append("custom_checklist", {"item": row.item, "required": row.required, "done": 0})


def enforce_checklist(doc, method=None):
	"""Block marking a task Done while a required checklist item is unticked. Fires
	on both close paths (modal save and the quick status dropdown both run validate)."""
	if doc.status != DONE_STATUS:
		return
	pending = [r.item for r in (doc.custom_checklist or []) if r.required and not r.done]
	if pending:
		frappe.throw(
			_("Cannot mark Done — {0} checklist item(s) still pending: {1}").format(
				len(pending), ", ".join(pending)
			),
			title=_("Checklist incomplete"),
		)


def enforce_location(doc, method=None):
	"""Fail-closed backstop for the location guard: an activity that requires a location (always
	in-person OR a matched conditional location_when) on a location-tracked lead grain must carry
	coordinates. The activity flow (save_activity) captures + writes them via the client; this
	guarantees the rule holds even on an API / import / scripted save where no browser ran. The
	gate lives once in location.api.location_required, fed by the reconstructed submitted values
	(one brain — same reconstruction the automation engine uses)."""
	from tatva_connect.activity.automation import reconstruct_values
	from tatva_connect.location.api import location_required

	# Location is captured when the visit is LOGGED (Done), not while the task is an open to-do.
	# An open/assigned in-person task legitimately has no coordinates yet — only block on completion.
	if doc.status != DONE_STATUS:
		return
	if doc.reference_doctype != "CRM Lead" or not doc.reference_docname:
		return
	values = reconstruct_values(doc)
	if location_required(doc.custom_task_type, doc.reference_docname, values) is None:
		return
	if not (doc.custom_location_latitude and doc.custom_location_longitude):
		frappe.throw(
			_("Capture your location at the doctor's site to complete this visit — mark it Done from the "
			  "Tasks list or open the task."),
			title=_("Location required"),
		)
	if not doc.custom_location_captured_at:
		doc.custom_location_captured_at = now_datetime()


def enforce_activity_logged(doc, method=None):
	"""Fail-closed guarantee: a form-activity task cannot be completed (Done) without its details
	logged. Holds on EVERY save path — the quick status dropdown / API / import all run validate,
	not just the Form-view controller. The clean UX (the client opens the activity form on
	completion) sits on top of this; if that UX ever breaks, completion degrades to a clear block,
	never a silent empty activity. One brain: activity.api.activity_is_unlogged owns the rule."""
	from tatva_connect.activity.api import activity_is_unlogged

	if activity_is_unlogged(doc):
		frappe.throw(
			_("Log this activity's details before marking it Done — open the task and fill its form."),
			title=_("Activity not logged"),
		)


@frappe.whitelist()
def create_followup_task(lead, task_type, due_in_hours=4, assigned_to=None, title=None, due_at=None):
	"""Idempotent follow-up task. Throttle: ONE open task per lead per type — if one
	is already open, return it untouched. Otherwise create it (assigned + due at
	`due_at` if given, else `due_in_hours` from now). Also the method the WhatsApp
	inbound event calls.

	Best-effort throttle: the check-then-insert isn't locked, so two near-simultaneous
	inbound messages for the same lead could rarely create two tasks. Acceptable — a
	duplicate follow-up is merely noisy, never wrong."""
	if not frappe.db.exists("CRM Lead", lead):
		frappe.throw(_("Lead {0} not found").format(lead))

	existing = frappe.db.get_value(
		"CRM Task",
		{
			"reference_doctype": "CRM Lead",
			"reference_docname": lead,
			"custom_task_type": task_type,
			"status": ["not in", CLOSED_STATUSES],
		},
		"name",
	)
	if existing:
		return existing

	due_date = due_at or add_to_date(now_datetime(), hours=cint(due_in_hours))
	task = frappe.get_doc(
		{
			"doctype": "CRM Task",
			"title": title or task_type,
			"custom_task_type": task_type,
			"status": "Todo",
			"due_date": due_date,
			"assigned_to": assigned_to,
			"reference_doctype": "CRM Lead",
			"reference_docname": lead,
			# Every caller of this helper is an automation (lead-assignment follow-up, activity
			# transition, WhatsApp inbound) -> stamp it so the timeline can badge it as automated.
			"custom_automated": 1,
		}
	)
	task.insert(ignore_permissions=True)
	return task.name


# -- helpers -----------------------------------------------------------------


def _lead_axes(doc):
	"""(vertical, group, program) of the linked lead, or blanks if not lead-linked."""
	if doc.reference_doctype == "CRM Lead" and doc.reference_docname:
		v = frappe.db.get_value(
			"CRM Lead",
			doc.reference_docname,
			["custom_vertical", "custom_group", "custom_current_program"],
			as_dict=True,
		)
		if v:
			return (v.custom_vertical or ""), (v.custom_group or ""), (v.custom_current_program or "")
	return "", "", ""


def resolve_template(task_type, vertical, group, program):
	"""Most-specific-wins checklist template for the lead's grain — a thin caller of
	the shared grain brain (taxonomy.grain.resolve_scoped). No global default; a set
	axis must match, a blank axis is a wildcard; an exact tie -> raise; no match -> None."""
	from tatva_connect.taxonomy.grain import resolve_scoped

	candidates = [
		{"name": c.name, "vertical": c.vertical, "group": c.psp_group, "program": c.program}
		for c in frappe.get_all(
			"CRM Task Checklist Template",
			filters={"task_type": task_type, "enabled": 1},
			fields=["name", "vertical", "psp_group", "program"],
		)
	]
	winner = resolve_scoped(candidates, vertical, group, program)
	return frappe.get_doc("CRM Task Checklist Template", winner["name"]) if winner else None
