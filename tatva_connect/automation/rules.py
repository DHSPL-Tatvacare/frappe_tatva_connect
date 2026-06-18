"""Rule selection + criteria matching for the automation engine.

The dispatcher (dispatcher.py) owns the trigger, the guarded executor, and the actions; this
module owns the two pure decisions in between: which enabled rules apply to a lead (grain fan-out,
ALL-MATCH — every rule whose specified axes equal the lead's, NOT resolve_scoped's single winner),
and whether a rule's criteria all match the trigger context. No side effects, no saves.

One brain: grain axes are read through the same accessor the activity engine uses
(activity.api._lead_axes); criteria reuse Frappe's native filter comparator (frappe.utils.data.compare)
so authoring matches list-view operator semantics. Nothing here is reimplemented.
"""
import frappe
from frappe.utils import compare

# Operators the native comparator handles directly (left op right). The list operators and the
# presence/range operators are handled locally below (they shape their operands first).
_NATIVE_OPS = {"=", "!=", "<", ">", "<=", ">=", "like", "not like"}


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


def matching_rules(vertical, group, program, task_type):
	"""Every ENABLED rule for this completed task's TYPE whose specified grain axes equal the lead's
	(blank axis = wildcard).

	ALL-MATCH fan-out (spec §5.1) — deliberately NOT resolve_scoped's pick-one. The trigger task type
	must match (a rule fires only for the activity type it was authored against). A rule with zero
	grain axes is rejected at author-time, so the query can never widen to a global default. Ordered
	by priority then creation so the executor fires them deterministically."""
	rows = frappe.db.sql(
		"""
		SELECT name, vertical, `group`, program, task_type, priority
		FROM `tabCRM Automation Rule`
		WHERE enabled = 1
		  AND task_type = %(tt)s
		  AND (vertical = '' OR vertical IS NULL OR vertical = %(v)s)
		  AND (`group`  = '' OR `group`  IS NULL OR `group`  = %(g)s)
		  AND (program  = '' OR program  IS NULL OR program  = %(p)s)
		ORDER BY priority ASC, creation ASC
		""",
		{"v": vertical or "", "g": group or "", "p": program or "", "tt": task_type or ""},
		as_dict=True,
	)
	return rows


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


def _one_match(c, context, ftype=None):
	"""Evaluate a single criterion against the context, type-aware (frappe casts both sides by the
	field's type: Datetime->get_datetime, Check->cint, numeric->flt) so a raw datetime/int from the
	activity form matches the typed criterion instead of silently failing."""
	left = context.get(c.field)
	op = c.operator
	if op == "is set":
		return left not in (None, "")
	if op == "is unset":
		return left in (None, "")
	try:
		if op in ("in", "not in"):
			items = [v.strip() for v in str(c.value or "").split(",")]
			return compare(str(left or ""), op, items)
		if op == "between":
			lo, hi = (str(c.value or "").split(",") + ["", ""])[:2]
			return _between(left, lo.strip(), hi.strip(), ftype)
		if op in _NATIVE_OPS:
			return compare(left, op, c.value, ftype)
	except Exception as e:
		# A comparison BUG must not masquerade as a clean non-match (which would silently kill the
		# rule with no trace). Log it (countable), then treat as non-match.
		frappe.log_error(
			title="automation: criterion eval failed",
			message="field={0} op={1} :: {2}".format(c.field, op, e),
		)
		return False
	return False


def _between(left, lo, hi, ftype=None):
	"""Inclusive range. Dates/datetimes are cast via get_datetime (a date literal vs a stored datetime
	would otherwise float()-fail then compare lexically); numbers via float; else string."""
	if left in (None, ""):
		return False
	if ftype in ("Datetime", "Date"):
		from frappe.utils import get_datetime

		try:
			return get_datetime(lo) <= get_datetime(left) <= get_datetime(hi)
		except Exception:
			return False
	try:
		return float(lo) <= float(left) <= float(hi)
	except (ValueError, TypeError):
		return str(lo) <= str(left) <= str(hi)
