"""The single grain-resolution brain.

A candidate carries a (vertical, group, program) scope. A SET axis must equal the
lead's value; a BLANK axis is a wildcard. Most-specific wins (program=4, group=2,
vertical=1); an exact tie at the top score is ambiguous -> raise. No match -> None.

Every grain-scoped config resolves through here: checklist templates
(tasks.resolve_template), activity-type availability (activity.api), and any future
grain config. Candidates are plain dicts exposing `vertical` / `group` / `program`.
"""
import frappe
from frappe import _

_WEIGHTS = (("program", 4), ("group", 2), ("vertical", 1))


def _score(candidate, vertical, group, program):
	"""Score a candidate against the lead axes, or None if a set axis mismatches."""
	lead = {"vertical": vertical or "", "group": group or "", "program": program or ""}
	score = 0
	for axis, weight in _WEIGHTS:
		cval = candidate.get(axis) or ""
		if cval:
			if cval != lead[axis]:
				return None
			score += weight
	return score


def resolve_scoped(candidates, vertical, group, program):
	"""Return the most-specific matching candidate (or None). Exact tie -> raise."""
	scored = [(s, c) for c in candidates if (s := _score(c, vertical, group, program)) is not None]
	if not scored:
		return None
	top = max(s for s, _ in scored)
	winners = [c for s, c in scored if s == top]
	if len(winners) > 1:
		frappe.throw(
			_("Ambiguous grain scope — {0} candidates are equally specific. Fix the scope.").format(len(winners)),
			title=_("Ambiguous scope"),
		)
	return winners[0]
