"""What each picker on an open ticket may offer: the ticket's grain as plain filters on every master that carries grain columns, and the titles of the values it holds."""
import frappe

from tatva_connect.api.list_link_titles import get_doc_link_titles
from tatva_connect.taxonomy import grain

TICKET = "HD Ticket"


@frappe.whitelist()
def ticket_pickers(name):
	"""One call per ticket load: `filters` per Link field whose master carries grain columns, and `titles` for the values the ticket holds."""
	frappe.has_permission(TICKET, "read", doc=name, throw=True)
	axes = grain.own_of(frappe.get_cached_doc(TICKET, name))
	return {"filters": grain_filters(axes), "titles": get_doc_link_titles(TICKET, name)}


@frappe.whitelist()
def new_ticket_pickers(fields):
	"""The new-ticket page's answer: the filters each picker takes once the agent has picked a grain, given as the ticket's own grain fields and clamped to the agent's entitlement."""
	from tatva_connect.taxonomy.picklist import _grain_from_axes

	frappe.has_permission(TICKET, "create", throw=True)
	picked = frappe.parse_json(fields) or {}
	axes = [picked.get(col) or "" if col else "" for col in grain.own_columns(TICKET)]
	return grain_filters(_grain_from_axes(frappe._dict(zip(grain.AXES, axes, strict=True))))


def grain_filters(axes):
	"""{fieldname: filters} for each ticket Link whose master carries grain columns; a blank axis on a row matches every grain, a blank axis on the ticket matches only those rows."""
	own = set(filter(None, grain.columns(TICKET, every=True)))  # the ticket's own grain fields are picked, never narrowed by themselves
	out = {}
	for df in frappe.get_meta(TICKET).fields:
		if df.fieldtype != "Link" or df.fieldname in own or df.options == TICKET:
			continue  # a link to another ticket (merged with, split from) is a record with its own grain, never a master to scope
		filters = {col: ["in", list(dict.fromkeys([value, ""]))]
		           for col, value in zip(grain.columns(df.options, every=True), axes, strict=True) if col}
		if filters:
			out[df.fieldname] = filters
	return out
