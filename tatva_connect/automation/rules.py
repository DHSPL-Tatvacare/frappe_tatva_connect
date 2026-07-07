"""Rule selection + criteria matching for the automation engine.

The dispatcher (dispatcher.py) owns the trigger, the guarded executor, and the actions; this
module owns the two pure decisions in between: which enabled rules apply to a lead (grain fan-out,
ALL-MATCH — every rule whose specified axes equal the lead's, NOT resolve_scoped's single winner),
and whether a rule's criteria all match the trigger context. No side effects, no saves.

One brain: grain axes are read through the same accessor the activity engine uses
(activity.api._lead_axes). Criteria cast through the ONE shared typed caster (`_cast`, built on
`frappe.utils.cast` - Date->getdate, Datetime->get_datetime, Float/Currency->flt, Int->cint, else
str) - every operator family (equality, ordering, membership, between, the change operators) routes
through it. No second casting path (A.8).
"""
import operator as _operator

import frappe


def grain_matches(row, vertical, group, program):
	"""The one grain predicate (allowlist checks + the describe builder): a grain row matches a target
	grain when each axis the row SETS equals the target's — a blank axis on the row is a wildcard.
	Same semantics as matching_rules' SQL, applied per-row in Python."""
	target = {"vertical": vertical or "", "group": group or "", "program": program or ""}
	for axis, tval in target.items():
		if (row.get(axis) or "") not in ("", tval):
			return False
	return True


def lead_axes(lead):
	"""(vertical, group, program) of a lead — the SAME accessor the activity engine uses (one brain)."""
	from tatva_connect.activity.api import _lead_axes

	return _lead_axes(lead)


def matching_rules(on_doctype, event, vertical, group, program):
	"""Every ENABLED rule whose (on_doctype, event) match the trigger and whose specified grain axes
	equal the lead's (blank axis = wildcard).

	ALL-MATCH fan-out (spec §5.1) - deliberately NOT resolve_scoped's pick-one. TATVA v2 (Task 4): the
	ONE matcher for every trigger shape (Created/Updated/Deleted) - REPLACES the v1 split
	(matching_rules(vertical, group, program, task_type) for Task-Completed and
	matching_rules_for_field_change(...) for Field-Changed; both retired with dispatcher.fire_rules /
	watch.py - see router.py). A rule with zero grain axes is rejected at author-time, so the query can
	never widen to a global default. Ordered by priority then creation so the executor fires them
	deterministically."""
	# Blank axis = wildcard: ["in", ["", None, x]] reproduces (= '' OR IS NULL OR = x); get_all
	# auto-quotes the reserved word `group`. Equivalence vs the old raw SQL verified on dev (0 NULLs,
	# identical row sets). Three AND-ed filter keys = the three AND-ed OR-groups.
	return frappe.get_all(
		"CRM Automation Rule",
		filters={
			"enabled": 1,
			"on_doctype": on_doctype,
			"event": event,
			"vertical": ["in", ["", None, vertical or ""]],
			"group": ["in", ["", None, group or ""]],
			"program": ["in", ["", None, program or ""]],
		},
		fields=["name", "vertical", "group", "program", "on_doctype", "event", "priority"],
		order_by="priority asc, creation asc",
	)


def criteria_match(criteria, context, field_types=None):
	"""True if EVERY criterion matches the context (spec §4). A rule with no criteria matches on
	grain + trigger alone. `field_types` maps fieldname -> schema type so comparisons evaluate
	type-aware (the same types the builder offers operators for — one brain). One bad criterion never
	raises — an unparseable compare is a non-match."""
	field_types = field_types or {}
	for c in criteria:
		if not _one_match(c, context, field_types.get(c.field)):
			return False
	return True


# The frozen v2 word-operator set (plan Part A), grouped by family so `_one_match` dispatches with a
# handful of dict lookups instead of a long if/elif ladder (A.12). Each family's compare logic lives
# in its own small helper below, all routed through the ONE shared caster `_cast` (A.8).
_EQUALITY_OPS = {"is": True, "is not": False}
_ORDER_OPS = {
	"greater than": _operator.gt,
	"less than": _operator.lt,
	"at least": _operator.ge,
	"at most": _operator.le,
}
_MEMBERSHIP_OPS = {"is one of": True, "is not one of": False}
_TEXT_OPS = {"contains": True, "does not contain": False}
_PRESENCE_OPS = {"is set": True, "is not set": False}
_CHANGE_OPS = {"changed to", "changed from…to"}


def _one_match(c, context, ftype=None):
	"""Evaluate a single criterion against the context, type-aware (every comparison casts through
	`_cast`/`_eq_typed` by the field's type, so a raw datetime/int from the trigger matches the typed
	criterion instead of silently failing). The two `changed…` operators read the watched field's
	`__before` context key (populated only on event=Updated) - a missing `__before` is a non-match,
	not a raise (fail-soft parity with every other operator). A comparison BUG (not a missing key) is
	logged (countable) then treated as a non-match - it must never masquerade as a clean pass."""
	left = context.get(c.field)
	op = c.operator
	try:
		if op in _PRESENCE_OPS:
			return (left not in (None, "")) == _PRESENCE_OPS[op]
		if op in _EQUALITY_OPS:
			return _eq_typed(left, c.value, ftype) == _EQUALITY_OPS[op]
		if op in _ORDER_OPS:
			return _ordered(left, c.value, ftype, _ORDER_OPS[op])
		if op in _MEMBERSHIP_OPS:
			return _in_list(left, c.value, ftype) == _MEMBERSHIP_OPS[op]
		if op in _TEXT_OPS:
			return _contains(left, c.value) == _TEXT_OPS[op]
		if op == "is between":
			return _between(left, c.from_value, c.value, ftype)
		if op in _CHANGE_OPS:
			return _changed_match(op, c, context, left, ftype)
	except Exception as e:
		frappe.log_error(
			title="automation: criterion eval failed",
			message=f"field={c.field} op={op} :: {e}",
		)
		return False
	return False


def _cast(value, ftype):
	"""The ONE typed caster every operator family routes through (Date->getdate, Datetime->
	get_datetime, Float/Currency->flt, Int->cint, else str - frappe.utils.cast). An uncastable value
	falls back to itself unchanged so the caller's own comparison decides the (fail-soft) verdict."""
	if not ftype:
		return value
	try:
		return frappe.utils.cast(ftype, value)
	except Exception:  # nosec B110 - fall through to an unconverted compare rather than raise
		return value


def _eq_typed(left, right, ftype=None):
	"""Type-aware equality - the shared comparator for `is`/`is not`, membership, and the change
	operators. Casts both sides via `_cast` so a stored Date matches a date literal and 7=='7'==7.0."""
	if ftype:
		return _cast(left, ftype) == _cast(right, ftype)
	return (left if left is not None else "") == (right if right is not None else "")


def _ordered(left, right, ftype, fn):
	"""`greater than`/`less than`/`at least`/`at most` - both sides cast via the shared `_cast`, then
	compared with the family's operator function. A blank operand or an incomparable cast (e.g. a
	string that didn't cast to a date) is a non-match, never a raise."""
	if left in (None, "") or right in (None, ""):
		return False
	try:
		return fn(_cast(left, ftype), _cast(right, ftype))
	except TypeError:
		return False


def _split_list(value):
	"""`is one of`/`is not one of` operand shaping - comma OR newline separated, blank items dropped."""
	return [v.strip() for v in str(value or "").replace("\n", ",").split(",") if v.strip()]


def _in_list(left, value, ftype):
	"""Membership via the shared `_eq_typed` per item - one cast path, no parallel list-compare."""
	if left in (None, ""):
		return False
	return any(_eq_typed(left, item, ftype) for item in _split_list(value))


def _contains(left, value):
	"""`contains`/`does not contain` - case-insensitive substring on text; no type cast (text fields
	only per Part A)."""
	return str(value or "").lower() in str(left or "").lower()


def _between(left, lo, hi, ftype=None):
	"""`is between` - inclusive range, both bounds cast via the shared `_cast` (Date/Datetime/numeric)."""
	if left in (None, "") or lo in (None, "") or hi in (None, ""):
		return False
	try:
		return _cast(lo, ftype) <= _cast(left, ftype) <= _cast(hi, ftype)
	except TypeError:
		return False


def _changed_match(op, c, context, left, ftype):
	"""`changed to` / `changed from…to` - both read the watched field's `__before` context key,
	populated only on event=Updated. A MISSING key (Created fire, stale/direct call) is a clean
	non-match, never a raise. `changed to` additionally requires the value actually moved (before !=
	after) so a same-value re-save doesn't falsely fire."""
	before_key = f"{c.field}__before"
	if before_key not in context:
		return False
	before = context.get(before_key)
	if op == "changed to":
		return not _eq_typed(before, left, ftype) and _eq_typed(left, c.value, ftype)
	return _eq_typed(before, c.from_value, ftype) and _eq_typed(left, c.value, ftype)
