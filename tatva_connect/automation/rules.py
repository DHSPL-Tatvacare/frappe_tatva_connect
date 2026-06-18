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


def criteria_match(criteria, context):
	"""True if EVERY criterion matches the context (spec §4). A rule with no criteria matches on
	grain + trigger alone. One bad criterion never raises — an unparseable compare is a non-match."""
	for c in criteria:
		if not _one_match(c, context):
			return False
	return True


def _one_match(c, context):
	"""Evaluate a single criterion against the context, using native filter-operator semantics."""
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
			return _between(left, lo.strip(), hi.strip())
		if op in _NATIVE_OPS:
			return compare(left, op, c.value)
	except Exception:
		return False
	return False


def _between(left, lo, hi):
	"""Inclusive numeric/string range — mirrors the list-view 'between' filter."""
	if left in (None, ""):
		return False
	try:
		return float(lo) <= float(left) <= float(hi)
	except (ValueError, TypeError):
		return str(lo) <= str(left) <= str(hi)
