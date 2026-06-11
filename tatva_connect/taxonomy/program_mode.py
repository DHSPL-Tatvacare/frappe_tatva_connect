"""Shared program-mode resolution — ONE brain for every lead-intake source.

A lead's program is resolved from the intake config's MODE, never hardcoded. Both
intake sources carry the same two knobs — a pinned `program` and an `allowed_programs`
list — so they resolve program identically:

  * FORCED - a program is pinned            -> use it.
  * LIST   - no pin + an allowed list        -> caller/form MUST supply one IN the list.
  * NONE   - no pin + no allowed list        -> no program.

Two mouths (partner API key, enrolment form), one brain: change this function and BOTH
paths change together — there is no second copy to drift. Identity is always
mobile + vertical + group; program is a mutable attribute resolved here.
"""
import frappe
from frappe import _


def resolve_program(forced_program, allowed_programs, submitted_program,
                    field_label="program", source_label="intake"):
	"""Resolve a lead's program from a routing config's mode.

	* forced_program   - the config's pinned program ("" / None = not forced).
	* allowed_programs - list of CRM Program names the source may set (empty = none).
	* submitted_program- what the caller/form supplied (may be blank).
	* field_label/source_label - shape the error text per caller (keeps messages exact).

	Returns the resolved program name, or None for NONE mode. Raises on a bad LIST pick.
	"""
	if forced_program:
		return forced_program  # FORCED
	submitted = (submitted_program or "").strip()
	if not allowed_programs:
		return None  # NONE
	if not submitted:
		frappe.throw(_("{0} is required for this {1}").format(field_label, source_label))
	if submitted not in allowed_programs:
		frappe.throw(_("Program '{0}' is not permitted for this {1}").format(submitted, source_label))
	return submitted  # LIST
