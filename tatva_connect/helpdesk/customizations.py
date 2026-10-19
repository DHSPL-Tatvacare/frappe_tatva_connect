"""An open ticket's form customizations: helpdesk's own, plus the Field Dependency scripts it ships only to the new-ticket page."""
import frappe


@frappe.whitelist()
def get_ticket_customizations():
	"""Stock customizations with every enabled Field Dependency script added, so an open ticket narrows a field as a new one does."""
	from helpdesk.helpdesk.doctype.hd_ticket.api import get_ticket_customizations as stock

	out = stock()
	# Helpdesk's own Field Dependencies list reads these rows by the same two marks: standard, and named for the pair.
	dependencies = frappe.get_all(  # authz-ok: tier-b — stock's @agent_only gate ran above; these are the scripts the new-ticket page already serves
		"HD Form Script",
		filters={"dt": "HD Ticket", "apply_to": "Form", "enabled": 1, "is_standard": 1, "apply_on_new_page": 1,
		         "apply_to_customer_portal": 0, "name": ["like", "Field Dependency-%"]},
		pluck="script",
	)
	out["_form_script"] = (out.get("_form_script") or []) + dependencies
	return out
