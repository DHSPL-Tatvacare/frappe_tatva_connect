"""Push triggers — notify the assigned Sales Rep on the two moments that matter.

  * New lead assigned to a rep  -> ToDo.after_insert (reference_type == CRM Lead)
  * New task assigned to a rep   -> CRM Task.after_insert

Both gate on the master switch first, then enqueue the actual FCM call on the short
queue so the save path is never blocked by a network round-trip. Logic stays here;
the registry row (`Push::FCM::notify`) is the single on/off.
"""
import frappe

from tatva_connect.push_notifications import sender


def _route_for_task(doc) -> str:
	ref_t, ref_n = doc.get("reference_type"), doc.get("reference_docname")
	if ref_t == "CRM Lead" and ref_n:
		return f"/crm/leads/{ref_n}"
	if ref_t == "CRM Deal" and ref_n:
		return f"/crm/deals/{ref_n}"
	return "/crm"


def on_task_created(doc, method=None):
	if not sender.is_enabled() or not doc.get("assigned_to"):
		return
	frappe.enqueue(
		"tatva_connect.push_notifications.sender.send_to_users",
		queue="short",
		users=[doc.assigned_to],
		title="New task assigned",
		body=doc.get("title") or "You have a new task",
		data={"doctype": "CRM Task", "name": doc.name, "route": _route_for_task(doc)},
	)


def on_lead_assigned(doc, method=None):
	# doc is the ToDo created by assignment; act only on CRM Lead assignments.
	if not sender.is_enabled() or doc.reference_type != "CRM Lead" or not doc.allocated_to:
		return
	lead_name = frappe.db.get_value("CRM Lead", doc.reference_name, "lead_name") or doc.reference_name
	frappe.enqueue(
		"tatva_connect.push_notifications.sender.send_to_users",
		queue="short",
		users=[doc.allocated_to],
		title="New lead assigned",
		body=lead_name,
		data={"doctype": "CRM Lead", "name": doc.reference_name, "route": f"/crm/leads/{doc.reference_name}"},
	)
