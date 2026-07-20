"""Rule selection + predicate evaluation. Two pure decisions, no side effects, no saves.

Which rules apply to a lead (grain fan-out, ALL-MATCH — every rule whose specified axes equal the
lead's, NOT resolve_scoped's single winner), and whether a predicate holds for a context.

TWO FAILURE POLICIES, AND THE DIFFERENCE IS DELIBERATE
------------------------------------------------------
An AUTHORING fault — a field the subject does not have, an operator nobody defines, a malformed tree —
raises `PredicateError`. A predicate that quietly returns False when it names a nonexistent field is
the worst bug this engine can have: the rule looks right, never fires, and says nothing. It is
indistinguishable from a condition that legitimately did not hold, so nobody ever finds it.

A COMPARISON that cannot be made — an uncastable value, a missing `__before` on a Created event — is a
clean non-match. That is data being data, not the author being wrong, and it must never block a save.

One brain: grain axes are read through the same accessor the activity engine uses
(activity.api._lead_axes). Criteria cast through the ONE shared typed caster (`_cast`, built on
`frappe.utils.cast` - Date->getdate, Datetime->get_datetime, Float/Currency->flt, Int->cint, else
str) - every operator family (equality, ordering, membership, between, the change operators) routes
through it. No second casting path (A.8).
"""
import operator as _operator

import frappe


def lead_axes(lead):
	"""(vertical, group, program) of a lead — the SAME accessor the activity engine uses (one brain)."""
	from tatva_connect.activity.api import _lead_axes

	return _lead_axes(lead)


class PredicateError(Exception):
	"""An authoring fault in a predicate — a field nobody declares, an unknown node, a malformed rule.

	It is an EXCEPTION and not a False on purpose. A predicate that silently evaluates to false when it
	names a field that does not exist is the single worst failure mode this engine can have: the author
	sees a rule that looks right, the run never fires, and nothing anywhere says why. Loud and stopped
	beats quiet and wrong."""


ALL, ANY, NOT, RULE = "all", "any", "not", "rule"
_GROUPS = (ALL, ANY, NOT)


def predicate_match(predicate, context, field_types=None):
	"""Evaluate a predicate tree against a context. The ONE evaluator, used by every consumer.

	A predicate is a tree of nodes, each carrying its `type`:

	    {"type": "rule", "field": "status", "operator": "is", "value": "New"}
	    {"type": "all",  "children": [ ... ]}     every child must hold
	    {"type": "any",  "children": [ ... ]}     at least one child must hold
	    {"type": "not",  "children": [ one ]}     the child must not hold

	An empty predicate is an open gate — it matches. An empty group matches too, so an author part-way
	through building one is not told their half-finished rule is false; the builder blocks the save
	instead, which is where an incomplete rule should surface.

	`field_types` maps fieldname -> schema type so every comparison is type-aware. Where it is given it
	is also the DECLARATION of what may be referenced: a rule naming a field outside it raises rather
	than quietly failing to match."""
	if not predicate:
		return True
	return _node_match(_as_node(predicate), context, field_types or {})


def _as_node(node):
	"""One shape in, whatever the caller stored. Raises on anything that is not a predicate node."""
	if not isinstance(node, dict):
		raise PredicateError(f"a predicate node must be an object, got {type(node).__name__}")
	node = frappe._dict(node)
	if node.type not in (*_GROUPS, RULE):
		raise PredicateError(f"unknown predicate node type {node.type!r}")
	return node


def _node_match(node, context, field_types):
	if node.type == RULE:
		return _rule_match(node, context, field_types)
	children = [_as_node(c) for c in (node.children or [])]
	if node.type == NOT:
		if len(children) != 1:
			raise PredicateError(f"a 'not' takes exactly one child, got {len(children)}")
		return not _node_match(children[0], context, field_types)
	if not children:
		return True
	# Eager, not short-circuit: an authoring fault must surface wherever it sits, whatever the data.
	verdicts = [_node_match(child, context, field_types) for child in children]
	return all(verdicts) if node.type == ALL else any(verdicts)


def _rule_match(rule, context, field_types):
	"""One leaf. The field must be something the subject actually has, and the operator must be real."""
	if not rule.field:
		raise PredicateError("a rule must name a field")
	known = field_types or context
	if rule.field not in known:
		raise PredicateError(
			f"{rule.field!r} is not a field of this subject — a predicate cannot test what does not exist"
		)
	if rule.operator not in KNOWN_OPERATORS:
		raise PredicateError(f"unknown operator {rule.operator!r} on {rule.field}")
	return _one_match(rule, context, (field_types or {}).get(rule.field))


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
_RANGE_OPS = {"is between"}
_CHANGE_OPS = {"changed to", "changed from…to"}

# Every operator that exists, composed from the same families describe.py offers per field type.
KNOWN_OPERATORS = frozenset({
	*_EQUALITY_OPS, *_ORDER_OPS, *_MEMBERSHIP_OPS, *_TEXT_OPS, *_PRESENCE_OPS, *_RANGE_OPS, *_CHANGE_OPS,
})


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
		if op in _RANGE_OPS:
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
	"""`changed to` / `changed from…to` - both read the watched field's `__before` key, populated only on
	event=Updated. A MISSING key (Created fire, stale/direct call) is a clean non-match, never a raise.
	`changed to` additionally requires the value actually moved (before != after) so a same-value re-save
	doesn't falsely fire.

	Composed as `<ref>__before`, which is the namespaced form unchanged: `refs` puts the suffix on the
	FIELD, so `crm_lead.status` pairs with `crm_lead.status__before` and this line needs no knowledge of
	the namespace at all. `refs.BEFORE` is the one spelling — `context_for` writes it and this reads it,
	and neither may spell it itself."""
	from tatva_connect.workflow_engine import refs

	before_key = f"{c.field}{refs.BEFORE}"
	if before_key not in context:
		return False
	before = context.get(before_key)
	if op == "changed to":
		return not _eq_typed(before, left, ftype) and _eq_typed(left, c.value, ftype)
	return _eq_typed(before, c.from_value, ftype) and _eq_typed(left, c.value, ftype)
