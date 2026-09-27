"""The agent's sub type picker: helpdesk keeps a snapshot of which sub types each type offers, and it is rebuilt from the rows whenever one changes."""
import frappe

PARENT_FIELD = "ticket_type"
CHILD_FIELD = "custom_ticket_sub_type"
SUB_TYPE = "HD Ticket Sub Type"


def mapping():
	"""Which sub types each ticket type offers, read from the rows; a retired row is offered to nobody."""
	out = {}
	for row in frappe.get_all(SUB_TYPE, filters={"disabled": 0}, fields=["name", "ticket_type"],
	                          order_by="ticket_type, name"):
		out.setdefault(row.ticket_type, []).append(row.name)
	return out


def rebuild(doc=None, method=None):
	"""Write the snapshot through helpdesk's own Field Dependency API; silent during install and migrate, where rows are still arriving."""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
		return
	from helpdesk.api.settings.field_dependency import create_update_field_dependency

	create_update_field_dependency(
		parent_field=PARENT_FIELD,
		child_field=CHILD_FIELD,
		parent_child_mapping=frappe.as_json(mapping()),
		enabled=1,
		fields_criteria=frappe.as_json({}),
	)
