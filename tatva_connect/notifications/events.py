"""Notification triggers — resolve the recipient + hand off to the ONE send path.

  * New lead assigned to a rep  -> ToDo.after_insert (reference_type == CRM Lead)
  * New task assigned to a rep   -> CRM Task.after_insert

Each handler does the minimum: figure out WHO + WHAT, then call `dispatch.notify`. The
global gate, opt-in filter, bell persistence, and FCM transport all live behind
`dispatch.notify` — none of that leaks back here (one brain, invariant A.8).
"""
import frappe

from tatva_connect.notifications import dispatch


def _route_for_task(doc) -> str:
	ref_t, ref_n = doc.get("reference_type"), doc.get("reference_docname")
	if ref_t == "CRM Lead" and ref_n:
		return f"/crm/leads/{ref_n}"
	if ref_t == "CRM Deal" and ref_n:
		return f"/crm/deals/{ref_n}"
	return "/crm"


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
