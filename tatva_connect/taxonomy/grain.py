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


AXES = ("vertical", "group", "program")

# The master each axis is named by. A grain value is that master's PRIMARY KEY, never free text.
_MASTER = {"vertical": "CRM Vertical", "group": "CRM Group", "program": "CRM Program"}


def same(a, b) -> bool:
	"""Are these two grain axis values the same value?

	MariaDB stores these under utf8mb4_unicode_ci and therefore calls 'GoodFlip' and 'Goodflip' ONE
	key; Python calls them two strings. Comparing them with `==` is how the app comes to believe a row
	is missing that the database will refuse to create — and, worse, how a rule scoped to a vertical
	silently fails to cover its own leads. The database owns the semantics; this matches it.
	"""
	return (a or "").casefold() == (b or "").casefold()


def canon(axis, value):
	"""The master's OWN spelling of this value — use it whenever a grain value is written or keyed on.

	`same` makes comparison agree with the database, but a value that is merely equal is not enough
	when it lands in a composite primary key (`{vertical}::{group}::{program}::...`): there the exact
	characters ARE the identity. Ask the master. An unknown value is returned untouched so a genuinely
	missing master still fails loudly at the Link check rather than being silently invented here.
	"""
	if not value:
		return ""
	return frappe.db.get_value(_MASTER[axis], value, "name") or value


def _score(candidate, vertical, group, program):
	"""Score a candidate against the lead axes, or None if a set axis mismatches."""
	lead = {"vertical": vertical or "", "group": group or "", "program": program or ""}
	score = 0
	for axis, weight in _WEIGHTS:
		cval = candidate.get(axis) or ""
		if cval:
			if not same(cval, lead[axis]):
				return None
			score += weight
	return score


def covers(candidate, vertical, group, program) -> bool:
	"""Does this candidate's scope COVER the given axes? The one wildcard-match predicate.

	A SET axis must equal; a BLANK axis is a wildcard, so entitlement to a whole vertical covers a group
	inside it and a rule with no program applies to every program. This is `_score`'s rule with the
	ranking dropped — exposed because callers that only ask "does it match?" were each writing their own
	loop, and a second copy of this rule is how 129 fields once stayed hidden from 1,894 leads while the
	tests stayed green.
	"""
	return _score(candidate, vertical, group, program) is not None


def overlaps(candidate, vertical, group, program) -> bool:
	"""Could this candidate's scope and the given scope ever describe the SAME record?

	`covers` asks about a real record and is one-directional: only the CANDIDATE may wildcard an axis,
	because a record never does. This asks about two RULES, where EITHER side may leave an axis blank
	meaning ANY — "which fields could a workflow declaring vertical=X ever be allowed to set", "which
	users could a workflow at this grain ever assign to".

	The distinction is not pedantry. Feeding a RULE grain to `covers` compares its blank axis as the
	literal empty string, so a workflow scoped to a whole vertical matches only contracts that are equally
	blank and is offered almost nothing — the same shape as the defect that once hid 129 fields from
	1,894 leads. A caller holding a rule grain asks THIS; a caller holding a lead's real axes asks `covers`.
	"""
	target = {"vertical": vertical or "", "group": group or "", "program": program or ""}
	for axis, _weight in _WEIGHTS:
		cval = candidate.get(axis) or ""
		if cval and target[axis] and not same(cval, target[axis]):
			return False
	return True


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
