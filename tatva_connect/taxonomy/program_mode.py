"""Shared program-mode resolution — ONE brain for every lead-intake source.

A lead's program is resolved from the intake config's MODE, never hardcoded. Both
intake sources carry the same two knobs — a pinned `program` and an `allowed_programs`
list — so they resolve program identically:

  * FORCED - a program is pinned            -> use it.
  * LIST   - no pin + an allowed list        -> caller/form MUST supply one IN the list.
  * LIST   - the same, `optional`            -> MAY supply one IN the list, or none at all.
  * NONE   - no pin + no allowed list        -> no program.

`optional` relaxes ONLY the "you must pick" refusal: an unlisted value is refused exactly as before, so
a source can never widen its own set. It exists because a program is sometimes decided in the CRM after
review, and an intake that has to wait for that decision is an intake that does not happen.

Two mouths (partner API key, enrolment form), one brain: change this function and BOTH
paths change together — there is no second copy to drift. Identity is always
mobile + vertical + group; program is a mutable attribute resolved here.
"""
from frappe import _


def resolve_program(forced_program, allowed_programs, submitted_program,
                    field_label="program", source_label="intake", optional=False):
	"""Resolve a lead's program from a routing config's mode.

	* forced_program   - the config's pinned program ("" / None = not forced).
	* allowed_programs - list of CRM Program names the source may set (empty = none).
	* submitted_program- what the caller/form supplied (may be blank).
	* field_label/source_label - shape the error text per caller (keeps messages exact).
	* optional         - LIST mode only: a blank submission resolves to None instead of refusing.

	Returns the resolved program name, or None for NONE mode and for an optional LIST blank. Raises on
	an unlisted LIST pick whether or not `optional` is set.
	"""
	from tatva_connect.api._base import throw_field

	if forced_program:
		return forced_program  # FORCED
	submitted = (submitted_program or "").strip()
	if not allowed_programs:
		return None  # NONE
	if not submitted:
		if optional:
			return None  # LIST, program optional: the CRM picks it after review
		throw_field(
			_("This {1} runs more than one programme, so it cannot pick for the caller. Send `{0}` as "
			  "one of: {2}.").format(field_label, source_label, ", ".join(allowed_programs)),
			[field_label],
		)
	if submitted not in allowed_programs:
		throw_field(
			_("`{0}` reads `{1}` and this {2} runs only: {3}. Send one of those values.").format(
				field_label, submitted, source_label, ", ".join(allowed_programs)),
			[field_label],
		)
	return submitted  # LIST
