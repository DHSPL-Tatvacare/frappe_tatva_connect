# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Carry every `location_when` string onto the three declared predicate columns.

`location_when` held a hand-typed `<field>==<value>` / `<field> in a|b` string parsed by
`location.api._match_condition` — a SECOND predicate language beside the rule seam, with its own operator
set and its own evaluator. Worse, an unparseable string returned False, which `location_required` read as
"location not required": a DECLARED condition silently disabled the gate and logged a visit audit row as
"Not Required" to match.

The condition is now the same three columns a rule row carries (`location_condition_field` ·
`location_operator` · `location_condition_value`), compiled by `_rule_atom` and evaluated by
`_field_visible`. This converts what operators already declared so no type loses its gate on the way.

The old string is parsed HERE, once, and this parser dies with the patch — it is deliberately not shared
with the engine, which no longer knows this syntax. The `location_when` column is left in place: it is
dropped from the doctype JSON so nothing reads it, and an actual column drop is a separate guarded step
(the same reasoning that deferred Phase 7 of the task-sections plan — drop last, once the read path is
proven everywhere).

A row that cannot be converted is REPORTED, never guessed at: it is written to the Error Log naming the
type and the string, because a location gate quietly dropped is exactly the failure this whole change
exists to end. Idempotent — a type that already carries a condition field is left alone.
"""
import frappe


def execute():
	if not frappe.db.has_column("CRM Task Type", "location_when"):
		return  # already retired on this site
	# `frappe.qb` and not `get_all`: `location_when` is gone from the doctype JSON by the time this runs, so a
	# meta-validating query would refuse the very column being retired. PyPika names the column directly —
	# the native door for a read an ORM call cannot express (CLAUDE.md, NO RAW SQL).
	task_type = frappe.qb.DocType("CRM Task Type")
	rows = (
		frappe.qb.from_(task_type)
		.select(task_type.name, task_type.location_when, task_type.location_condition_field)
		.where(task_type.location_when.notnull() & (task_type.location_when != ""))
	).run(as_dict=True)
	unconvertible = []
	for row in rows:
		if (row.location_condition_field or "").strip():
			continue  # already converted; a second run must not overwrite an operator's edit
		parsed = _parse(row.location_when)
		if not parsed:
			unconvertible.append((row.name, row.location_when))
			continue
		field, operator, value = parsed
		frappe.db.set_value("CRM Task Type", row.name, {  # authz-ok: tier-c — patch, no session user
			"location_condition_field": field,
			"location_operator": operator,
			"location_condition_value": value,
		}, update_modified=False)

	if unconvertible:
		frappe.log_error(
			title="convert_location_when_to_a_predicate: unconverted conditions",
			message="These task types carry a location_when this patch could not parse, so their location "
					"gate is now inert and must be re-declared under Enforcement:\n\n"
					+ "\n".join(f"{name}: {when!r}" for name, when in unconvertible),
		)
	frappe.db.commit()


def _parse(when):
	"""The retired grammar, read one last time: `<field>==<value>` or `<field> in a|b|c`.

	`in` maps to the rule vocabulary's `is` against the FIRST value only when there is exactly one choice —
	a multi-value condition has no single-row equivalent, so it is reported rather than silently narrowed.
	D-N is the precedent on the rules side: a multi-value condition is N rows, and the Enforcement block
	carries one condition, so such a case is an operator decision and not a patch's to make."""
	when = (when or "").strip()
	if not when:
		return None
	if "==" in when:
		field, _, value = when.partition("==")
		return (field.strip(), "is", value.strip()) if field.strip() else None
	if " in " in when:
		field, _, opts = when.partition(" in ")
		choices = [o.strip() for o in opts.split("|") if o.strip()]
		if field.strip() and len(choices) == 1:
			return field.strip(), "is", choices[0]
	return None
