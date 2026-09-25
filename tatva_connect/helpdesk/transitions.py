"""The ticket lifecycle, read from `HD Ticket Transition` and nowhere else: which move is allowed, who may make it, and what it demands first."""
import frappe
from frappe import _

from tatva_connect.api._base import throw_by_audience

TRANSITION = "HD Ticket Transition"
TICKET = "HD Ticket"


def configured():
	"""True once an operator has written a rulebook. Until then a ticket moves exactly as stock helpdesk moves it."""
	return bool(frappe.db.count(TRANSITION, {"enabled": 1}))


def label_of(fieldname):
	"""The label an operator reads for a ticket column — asked of the meta, never typed beside it."""
	field = frappe.get_meta(TICKET).get_field(fieldname)
	return _(field.label) if field and field.label else fieldname


def guard(doc):
	"""Refuse a status change the rulebook does not carry, a role may not make, or that leaves a demanded field empty."""
	if doc.is_new() or not doc.has_value_changed("status") or not configured():
		return
	before = doc.get_doc_before_save()
	move = frappe.db.get_value(
		TRANSITION, {"from_status": before.status, "to_status": doc.status, "enabled": 1}, "name"
	)
	if not move:
		throw_by_audience(
			_("A ticket cannot move from {0} to {1}. The moves allowed from {0} are listed in Ticket Transitions.")
			.format(before.status, doc.status),
			_("`status` cannot move from `{0}` to `{1}`: no enabled HD Ticket Transition carries that move.")
			.format(before.status, doc.status),
			["status"], frappe.PermissionError,
		)
	rule = frappe.get_cached_doc(TRANSITION, move)
	if rule.allowed_role and rule.allowed_role not in frappe.get_roles():
		throw_by_audience(
			_("Moving a ticket from {0} to {1} is done by {2}.").format(before.status, doc.status, rule.allowed_role),
			_("`status` cannot move from `{0}` to `{1}` with this key: the move is reserved for the role `{2}`.")
			.format(before.status, doc.status, rule.allowed_role),
			["status"], frappe.PermissionError,
		)
	missing = [row.fieldname for row in rule.required_fields if not doc.get(row.fieldname)]
	if missing:
		throw_by_audience(
			_("Fill {0} before moving the ticket to {1}.")
			.format(", ".join(label_of(f) for f in missing), doc.status),
			_("`status` cannot move to `{0}` while {1} {2} empty.")
			.format(doc.status, ", ".join(f"`{f}`" for f in missing), _("is") if len(missing) == 1 else _("are")),
			["status", *missing],
		)
