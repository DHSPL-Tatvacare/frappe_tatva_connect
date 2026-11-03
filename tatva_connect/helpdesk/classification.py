"""Ticket Type and Ticket Sub Type: the pair a ticket may carry, and the picker the agent chooses it from, both read off the sub type rows."""
import frappe
from frappe import _

from tatva_connect.api._base import throw_by_audience
from tatva_connect.helpdesk import SETTINGS, SUB_TYPE

PARENT_FIELD = "ticket_type"
CHILD_FIELD = "custom_ticket_sub_type"
PRIORITY_FIELD = "default_priority"
PRIORITY_SWITCH = "custom_apply_sub_type_priority"


def mapping():
	"""Which sub types each ticket type offers, read from the rows; a retired row is offered to nobody."""
	out = {}
	for row in frappe.get_all(SUB_TYPE, filters={"disabled": 0}, fields=["name", "ticket_type"],
	                          order_by="ticket_type, name"):
		out.setdefault(row.ticket_type, []).append(row.name)
	return out


def rebuild():
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


def validate_pair(doc):
	"""A sub type declares the type it belongs to; the picker filters on it and this is the same rule on the write."""
	sub_type, ticket_type = doc.get(CHILD_FIELD), doc.get(PARENT_FIELD)
	if not sub_type:
		return
	if not (doc.has_value_changed(CHILD_FIELD) or doc.has_value_changed(PARENT_FIELD)):
		return
	owner = frappe.db.get_value(SUB_TYPE, sub_type, PARENT_FIELD)
	if owner == ticket_type:
		return
	throw_by_audience(
		_("{0} belongs to the ticket type {1}. Set that type, or pick a sub type of {2}.").format(
			sub_type, owner, ticket_type or _("this ticket's type")),
		_("`ticket_sub_type` reads `{0}`, which belongs to the ticket type `{1}`. Send that `ticket_type`, "
		  "or a sub type of `{2}`.").format(sub_type, owner, ticket_type or ""),
		["ticket_sub_type", "ticket_type"],
	)


def apply_priority(doc):
	"""A sub type may name the priority its tickets carry; it speaks only as the sub type changes, so a later choice of the agent's stands."""
	sub_type = doc.get(CHILD_FIELD)
	if not sub_type or not doc.has_value_changed(CHILD_FIELD):
		return
	if not frappe.db.get_single_value(SETTINGS, PRIORITY_SWITCH):
		return
	doc.priority = frappe.db.get_value(SUB_TYPE, sub_type, PRIORITY_FIELD) or doc.priority


def refuse_twin(doc, *identity):
	"""A row is unique within its business line by `identity` (its label by default); an older row keeps its plain name, so the key alone cannot see the twin."""
	from tatva_connect.taxonomy import grain

	identity = identity or ("label",)
	axes = {col: doc.get(col) or "" for col in grain.columns(doc.doctype, every=True) if col}
	twin = frappe.db.get_value(doc.doctype, {**axes, **{f: doc.get(f) or "" for f in identity}, "name": ["!=", doc.name]}, "name")
	if twin:
		said = " → ".join(doc.get(f) or _("any status") for f in identity)
		frappe.throw(_("{0} already has {1} for this business line.").format(_(doc.doctype), frappe.bold(said)))


def refuse_other_line(doc):
	"""A value a person picks must be set up for the ticket's business line, as its picker offers it; a value the system fills is checked where that is set up."""
	from tatva_connect.helpdesk.pickers import grain_filters
	from tatva_connect.helpdesk.transitions import a_person_chose_it
	from tatva_connect.taxonomy import grain

	if not a_person_chose_it(doc):
		return
	moved = any(doc.has_value_changed(col) for col in grain.columns(doc.doctype, every=True) if col)
	for fieldname, filters in grain_filters(grain.own_of(doc)).items():
		value = doc.get(fieldname)
		if not value or not (moved or doc.has_value_changed(fieldname)):
			continue
		target = doc.meta.get_field(fieldname).options
		if frappe.db.exists(target, {"name": value, **filters}):
			continue
		title = frappe.get_meta(target).title_field
		shown = (title and frappe.db.get_value(target, value, title)) or value
		throw_by_audience(
			_("{0} is set up for another business line, not this ticket's. Pick one this ticket offers.").format(frappe.bold(shown)),
			_("`{0}` reads `{1}`, which is set up for another business line than this ticket's.").format(fieldname, value),
			[fieldname],
		)


def keep_default_type_on_every_line(doc, method=None):
	"""HD Ticket Type validate: the type HD Settings gives every new ticket must hold for every business line, or other lines' tickets are refused."""
	from tatva_connect.taxonomy import grain

	if doc.name != frappe.db.get_single_value(SETTINGS, "default_ticket_type"):
		return
	if any(doc.get(col) for col in grain.columns(doc.doctype, every=True) if col):
		frappe.throw(_("{0} is the default type of every new ticket, so it must hold for every business line.").format(frappe.bold(doc.name)))
