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
	if doc.flags.get(CUSTOMER_REPLY):
		# Helpdesk's own answer to inbound mail. A move the rulebook does not carry is dropped, never refused:
		# refusing raises inside the email pull, which loses the reply and stalls every later message.
		if not _enabled_rule(before, doc.status):
			doc.status = before
		return
	rule = _rule(before, doc.status)
	_within_reach_of_the_caller(rule, before, doc.status)
	_demands_are_met(rule, doc)


def moves_open_from(status):
	"""The statuses this one leads to, in name order, and only those the caller's roles reach."""
	roles = set(frappe.get_roles())
	open_to = [row.to_status for row in frappe.get_all(
		TRANSITION, filters={"from_status": status, "enabled": 1},
		fields=["to_status", "allowed_role"], order_by="to_status")
		if not row.allowed_role or row.allowed_role in roles]
	return _spoken_list(open_to, _("or"))


def _spoken_list(items, joiner):
	"""A list as a person reads it: `a`, `a and b`, `a, b and c`; empty when there is nothing to name."""
	if not items:
		return ""
	if len(items) == 1:
		return items[0]
	return f"{', '.join(items[:-1])} {joiner} {items[-1]}"


def _enabled_rule(before, after):
	"""The enabled row for this move, or None."""
	name = move_name(before, after)
	rule = frappe.get_cached_doc(TRANSITION, name) if frappe.db.exists(TRANSITION, name) else None
	return rule if rule and rule.enabled else None


def _rule(before, after):
	"""The enabled row for this move, or the refusal that no such move exists."""
	rule = _enabled_rule(before, after)
	if not rule:
		instead = moves_open_from(before)
		throw_by_audience(
			_("{0} does not lead to {1} — only to {2}.").format(before, after, instead) if instead
			else _("{0} does not lead to {1}, or anywhere else.").format(before, after),
			_("`status` cannot move from `{0}` to `{1}`: no enabled HD Ticket Transition carries that move.")
			.format(before, after),
			["status"],
		)
	return rule


def _within_reach_of_the_caller(rule, before, after):
	"""A move may be reserved for one role; a blank role is anyone's to make."""
	if rule.allowed_role and rule.allowed_role not in frappe.get_roles():
		instead = moves_open_from(before)
		throw_by_audience(
			_("{0} to {1} needs the {2} role. You can move it to {3}.").format(before, after, rule.allowed_role, instead)
			if instead else _("{0} to {1} needs the {2} role.").format(before, after, rule.allowed_role),
			_("`status` cannot move from `{0}` to `{1}` with this key: the move is reserved for the role `{2}`.")
			.format(before, after, rule.allowed_role),
			["status"], frappe.PermissionError,
		)


def _demands_are_met(rule, doc):
	"""Every field the move names must carry a value; the refusal names them as the operator and the caller each read them."""
	missing = [row.fieldname for row in rule.required_fields if not doc.get(row.fieldname)]
	if not missing:
		return
	throw_by_audience(
		_("{0} needs {1}.").format(doc.status, _spoken_list([label_of(f) for f in missing], _("and"))),
		_("`status` cannot move to `{0}` until {1} carries a value.")
		.format(doc.status, ", ".join(f"`{f}`" for f in missing)),
		["status", *missing],
	)
