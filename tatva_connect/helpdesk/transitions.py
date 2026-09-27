"""The ticket lifecycle, read from `HD Ticket Transition` and nowhere else: which move is allowed, who may make it, what it demands, and finality as the absence of a row."""
import frappe
from frappe import _

from tatva_connect.api._base import throw_by_audience
from tatva_connect.helpdesk import TICKET, TRANSITION

# Helpdesk moves a ticket itself when mail arrives; the flag says so, and only the field demands are waived.
CUSTOMER_REPLY = "customer_reply"


def rulebook_is_written():
	"""True once an operator has enabled one move. Until then this module refuses nothing."""
	return bool(frappe.db.count(TRANSITION, {"enabled": 1}))


def move_name(from_status, to_status):
	"""A move's primary key IS the move: `HD Ticket Transition` is autonamed `{from_status}::{to_status}`."""
	return f"{from_status}::{to_status}"


def previous_status(doc):
	"""The status this save moves away from: the copy frappe loaded before the write, or the stored row when it has none."""
	before = doc.get_doc_before_save()
	return before.status if before else frappe.db.get_value(TICKET, doc.name, "status")


def label_of(fieldname):
	"""The label an operator reads for a ticket column — asked of the meta, never typed beside it."""
	field = frappe.get_meta(TICKET).get_field(fieldname)
	return _(field.label) if field and field.label else fieldname


def guard(doc):
	"""Refuse a status change the rulebook does not carry, a role may not make, or that leaves a demanded field empty."""
	if doc.is_new() or not doc.has_value_changed("status") or not rulebook_is_written():
		return
	before = previous_status(doc)
	if not before or before == doc.status:  # nothing moved: a first save, or a save that restates the status
		return
	rule = _rule(before, doc.status)
	if doc.flags.get(CUSTOMER_REPLY):
		return  # the move is helpdesk's answer to inbound mail; refusing it would lose the customer's reply
	_within_reach_of_the_caller(rule, before, doc.status)
	_demands_are_met(rule, doc, before)


def _rule(before, after):
	"""The enabled row for this move, or the refusal that no such move exists."""
	name = move_name(before, after)
	rule = frappe.get_cached_doc(TRANSITION, name) if frappe.db.exists(TRANSITION, name) else None
	if not rule or not rule.enabled:
		throw_by_audience(
			_("{0} to {1} is not a move this ticket can make.").format(before, after),
			_("`status` cannot move from `{0}` to `{1}`: no enabled HD Ticket Transition carries that move.")
			.format(before, after),
			["status"],
		)
	return rule


def _within_reach_of_the_caller(rule, before, after):
	"""A move may be reserved for one role; a blank role is anyone's to make."""
	if rule.allowed_role and rule.allowed_role not in frappe.get_roles():
		throw_by_audience(
			_("{0} to {1} is a move only {2} can make.").format(before, after, rule.allowed_role),
			_("`status` cannot move from `{0}` to `{1}` with this key: the move is reserved for the role `{2}`.")
			.format(before, after, rule.allowed_role),
			["status"], frappe.PermissionError,
		)


def _demands_are_met(rule, doc, before):
	"""Every field the move names must carry a value; the refusal names them as the operator and the caller each read them."""
	missing = [row.fieldname for row in rule.required_fields if not doc.get(row.fieldname)]
	if not missing:
		return
	throw_by_audience(
		_("{0} to {1} needs {2}.")
		.format(before, doc.status, ", ".join(label_of(f) for f in missing)),
		_("`status` cannot move to `{0}` until {1} carries a value.")
		.format(doc.status, ", ".join(f"`{f}`" for f in missing)),
		["status", *missing],
	)
