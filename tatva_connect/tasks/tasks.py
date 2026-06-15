"""CRM Task automations: seed + enforce checklists, idempotent follow-up helper."""
import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

DONE_STATUS = "Done"
CLOSED_STATUSES = ("Done", "Canceled")
CALL_LEAD_TYPE = "Call Lead"
_SWITCH = ("Tatva Automation Settings", "enable_followup_task_automation")


def on_lead_assignment(doc, method=None):
	"""ToDo.after_insert — when a CRM Lead is assigned to an agent, raise ONE open
	'Call Lead' task for that agent (the on-lead-create follow-up). Fires after the
	Assignment Rule sets the owner; gated by the master switch (OFF by default)."""
	if doc.reference_type != "CRM Lead" or not doc.allocated_to:
		return
	if not frappe.db.get_single_value(*_SWITCH):
		return
	lead_name = frappe.db.get_value("CRM Lead", doc.reference_name, "lead_name") or doc.reference_name
	create_followup_task(
		lead=doc.reference_name,
		task_type=CALL_LEAD_TYPE,
		due_in_hours=24,
		assigned_to=doc.allocated_to,
		title=_("Call lead — {0}").format(lead_name),
	)


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
	"""Fail-closed backstop for the location guard: an in-person activity on a location-tracked
	lead grain must carry coordinates. The activity flow (save_activity) captures + writes them
	via the client; this guarantees the rule holds even on an API / import / scripted save where
	no browser ran. The gate lives once in location.api.location_guard_applies (one brain)."""
	from tatva_connect.location.api import location_guard_applies

	if doc.reference_doctype != "CRM Lead" or not doc.reference_docname:
		return
	if location_guard_applies(doc.custom_task_type, doc.reference_docname) is None:
		return
	if not (doc.custom_location_latitude and doc.custom_location_longitude):
		frappe.throw(
			_("This in-person activity requires a captured location. Open it in the CRM and save to capture."),
			title=_("Location required"),
		)
	if not doc.custom_location_captured_at:
		doc.custom_location_captured_at = now_datetime()


@frappe.whitelist()
def create_followup_task(lead, task_type, due_in_hours=4, assigned_to=None, title=None):
	"""Idempotent follow-up task. Throttle: ONE open task per lead per type — if one
	is already open, return it untouched. Otherwise create it (assigned + due in
	`due_in_hours`). Also the method the WhatsApp inbound event calls.

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

	task = frappe.get_doc(
		{
			"doctype": "CRM Task",
			"title": title or task_type,
			"custom_task_type": task_type,
			"status": "Todo",
			"due_date": add_to_date(now_datetime(), hours=cint(due_in_hours)),
			"assigned_to": assigned_to,
			"reference_doctype": "CRM Lead",
			"reference_docname": lead,
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
