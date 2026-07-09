"""The ONE query brain over `CRM Automation Field` — the merged field allowlist (read + write).

Every read of the allowlist goes through here so there is exactly one place that knows the table's
shape. Three capabilities live on one row via three flags:
  • can_read  — a rule criterion may test this field (grain-independent).
  • can_watch — a change to this field may fire a rule (grain-independent). Implies can_read: the
    engine already lifts a watched field's before/after pair into the criterion context.
  • can_set   — this field may be written by a Set Field / child-row action (grain-scoped).

Read and watch are distinct permissions: watching drives DISPATCH (only a changed watched field wakes
the router), reading only widens the criterion vocabulary. A per-task-type activity field is readable
but never watchable — it is not a column, so no save can diff it.

Grain is a SET scope only (a read/watch row is validated to have blank grain). So the read and watch
queries ignore grain; the set reads honour it via `rules.grain_matches` — the same predicate
rule-selection uses (one grain brain). This module replaces the two copies of `_allowlisted` that lived
in the dispatcher and the rule controller.
"""
import frappe

DOCTYPE = "CRM Automation Field"


# -- read + watch side (grain-independent) -----------------------------------


def readable_fields(doctype):
	"""The enabled fieldnames a rule criterion may test — the builder's vocabulary and the validator's
	fence. can_watch is folded in here (and nowhere else) because it implies can_read."""
	return frappe.get_all(
		DOCTYPE,
		filters={"doctype_name": doctype, "enabled": 1},
		or_filters={"can_read": 1, "can_watch": 1},
		pluck="fieldname",
	)


def is_watchable(doctype, fieldname):
	"""True if an enabled can_watch row exists for this parent field — what a transition operator
	(`changed to` / `changed from…to`) needs, since only a watched field carries a before-value."""
	return bool(
		frappe.db.exists(
			DOCTYPE, {"doctype_name": doctype, "fieldname": fieldname, "can_watch": 1, "enabled": 1}
		)
	)


def watchable_fields(doctype):
	"""The enabled can_watch fieldnames for a doctype (the dispatch diff cache)."""
	return frappe.get_all(
		DOCTYPE,
		filters={"doctype_name": doctype, "can_watch": 1, "enabled": 1},
		pluck="fieldname",
	)


# -- set side (grain-scoped) -------------------------------------------------


def is_settable(doctype, fieldname, axes, child_table_field="", require_row_key=False):
	"""Runtime/author write-gate: an enabled can_set row for {doctype, child, field} whose set axes
	equal the lead's grain (blank axis = wildcard) authorises the write. For a child-row write the row
	must also match the child table (and be a row key when required). Fail-closed."""
	from tatva_connect.automation import rules

	filters = {"doctype_name": doctype, "fieldname": fieldname, "can_set": 1, "enabled": 1}
	filters["child_table_field"] = child_table_field or ""
	if require_row_key:
		filters["is_row_key"] = 1
	rows = frappe.get_all(DOCTYPE, filters=filters, fields=["vertical", "group", "program"])
	return any(rules.grain_matches(row, axes[0], axes[1], axes[2]) for row in rows)


def settable_rows(doctype, axes):
	"""Enabled can_set PARENT rows (child_table_field blank) for a doctype whose grain matches — the
	raw rows the describe endpoint enriches for the Set Field target dropdown."""
	from tatva_connect.automation import rules

	return [
		r
		for r in frappe.get_all(
			DOCTYPE,
			filters={"doctype_name": doctype, "can_set": 1, "enabled": 1, "child_table_field": ""},
			fields=["fieldname", "vertical", "group", "program"],
		)
		if rules.grain_matches(r, axes[0], axes[1], axes[2])
	]
