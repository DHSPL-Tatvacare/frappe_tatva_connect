"""WhatsApp automations — driven by the WhatsApp Message row, not the provider.

Any WhatsApp Message of type "Incoming" linked to a CRM Lead raises ONE open
follow-up task for that lead's owner. Provider-agnostic: WATI (or any future
provider) just writes the message row; this event does the rest.
"""
import frappe
from frappe import _

WHATSAPP_FOLLOWUP_TYPE = "WhatsApp Follow-up"
FOLLOWUP_DUE_HOURS = 4


def on_inbound_message(doc, method=None):
	if (doc.type or "") != "Incoming":
		return
	if doc.reference_doctype != "CRM Lead" or not doc.reference_name:
		return

	owner = frappe.db.get_value("CRM Lead", doc.reference_name, "lead_owner")
	if not owner:
		return  # unowned lead — assignment must set an owner first; no task yet

	from tatva_connect.automation import tasks

	lead_name = frappe.db.get_value("CRM Lead", doc.reference_name, "lead_name") or doc.reference_name
	tasks.create_followup_task(
		lead=doc.reference_name,
		task_type=WHATSAPP_FOLLOWUP_TYPE,
		due_in_hours=FOLLOWUP_DUE_HOURS,
		assigned_to=owner,
		title=_("Reply to WhatsApp — {0}").format(lead_name),
	)
