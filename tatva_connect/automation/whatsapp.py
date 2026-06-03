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

	lead = frappe.db.get_value(
		"CRM Lead", doc.reference_name, ["lead_owner", "lead_name"], as_dict=True
	)
	if not (lead and lead.lead_owner):
		return  # unowned lead — assignment must set an owner first; no task yet

	from tatva_connect.automation import tasks

	tasks.create_followup_task(
		lead=doc.reference_name,
		task_type=WHATSAPP_FOLLOWUP_TYPE,
		due_in_hours=FOLLOWUP_DUE_HOURS,
		assigned_to=lead.lead_owner,
		title=_("Reply to WhatsApp — {0}").format(lead.lead_name or doc.reference_name),
	)
