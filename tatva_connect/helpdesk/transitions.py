"""The ticket lifecycle, read from `HD Ticket Transition` and nowhere else: a GATE on reaching a status — who may reach it, and what it demands first.

A row is a gate, never a permit. A status no row names is open: any move to it is allowed. That is the
whole reason this engine is reliable — the rulebook holds the handful of restrictions the business
actually has, not the full matrix of moves, so a pair nobody thought to write is free rather than frozen.

And the rulebook judges ONE actor: a person choosing a status in the app. Everything else moves a ticket
as a CONSEQUENCE rather than a choice — helpdesk reacting to mail, a workflow, a gated server lane — and
a consequence cannot be refused without losing the thing that caused it.
"""
import frappe
from frappe import _

from tatva_connect.api._base import in_partner_lane, throw_by_audience
from tatva_connect.helpdesk import TICKET, TRANSITION

# Helpdesk moves a ticket itself when mail arrives; the flag says so, and such a move is never judged.
CUSTOMER_REPLY = "customer_reply"

# A gate with no `from_status` covers every status, the way a blank axis on a grain rule means ANY.
ANY_STATUS = ""


def rulebook_is_written():
	"""True once an operator has enabled one gate. Until then this module refuses nothing."""
	return bool(frappe.db.count(TRANSITION, {"enabled": 1}))


def move_name(from_status, to_status):
	"""A gate's primary key IS the move: `HD Ticket Transition` is autonamed `{from_status}::{to_status}`."""
	return f"{from_status}::{to_status}"


def previous_status(doc):
	"""The status this save moves away from: the copy frappe loaded before the write, or the stored row when it has none."""
	before = doc.get_doc_before_save()
	return before.status if before else frappe.db.get_value(TICKET, doc.name, "status")


def label_of(fieldname):
	"""The label an operator reads for a ticket column — asked of the meta, never typed beside it."""
	field = frappe.get_meta(TICKET).get_field(fieldname)
	return _(field.label) if field and field.label else fieldname


def a_person_chose_it(doc):
	"""Did a human pick this status in the app?

	Three lanes move a ticket without anyone choosing to, and each would be a defect to refuse:
	helpdesk answering inbound mail (refusing raises inside the email pull and loses the reply), the
	workflow engine acting on a journey, and the partner API acting for a gated caller. Each is read
	through the narrowest signal it sets, never `ignore_permissions`: that flag is ambient, half the
	server runs under it, and reading it here would switch the rulebook off wherever it happened to be set.
	"""
	return not (doc.flags.get(CUSTOMER_REPLY) or frappe.flags.get("in_workflow") or in_partner_lane())


def gate_on(before, after):
	"""The enabled gate on reaching `after`: the one named for this exact move, else the wildcard, else None.

	Most specific wins, so a status can be open from everywhere but one place. `None` means no gate, which
	means the move is allowed — the inversion this engine turns on.
	"""
	for name in (move_name(before, after), move_name(ANY_STATUS, after)):
		if frappe.db.exists(TRANSITION, name):
			rule = frappe.get_cached_doc(TRANSITION, name)
			if rule.enabled:
				return rule
	return None


def guard(doc):
	"""Refuse a status a PERSON chose that its gate reserves for another role, or that leaves a demanded field empty."""
	if doc.is_new() or not doc.has_value_changed("status") or not rulebook_is_written():
		return
	if not a_person_chose_it(doc):
		return
	before = previous_status(doc)
	if not before or before == doc.status:  # nothing moved: a first save, or a save that restates the status
		return
	rule = gate_on(before, doc.status)
	if rule is None:
		return  # no gate on this status: the move is the caller's to make
	_within_reach_of_the_caller(rule, doc.status)
	_demands_are_met(rule, doc)


def _within_reach_of_the_caller(rule, after):
	"""A gate may reserve its status for one role; a blank role is anyone's to reach."""
	if rule.allowed_role and rule.allowed_role not in frappe.get_roles():
		throw_by_audience(
			_("Moving a ticket to {0} needs the {1} role.").format(after, rule.allowed_role),
			_("`status` cannot move to `{0}` with this key: that status is reserved for the role `{1}`.")
			.format(after, rule.allowed_role),
			["status"], frappe.PermissionError,
		)


def _demands_are_met(rule, doc):
	"""Every field the gate names must carry a value; the refusal names them as the operator and the caller each read them."""
	missing = [row.fieldname for row in rule.required_fields if not doc.get(row.fieldname)]
	if not missing:
		return
	throw_by_audience(
		_("{0} needs {1}.").format(doc.status, _spoken_list([label_of(f) for f in missing], _("and"))),
		_("`status` cannot move to `{0}` until {1} carries a value.")
		.format(doc.status, ", ".join(f"`{f}`" for f in missing)),
		["status", *missing],
	)


def _spoken_list(items, joiner):
	"""A list as a person reads it: `a`, `a and b`, `a, b and c`; empty when there is nothing to name."""
	if not items:
		return ""
	if len(items) == 1:
		return items[0]
	return f"{', '.join(items[:-1])} {joiner} {items[-1]}"
