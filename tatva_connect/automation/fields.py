"""The ONE query brain over `CRM Automation Field` — the merged field allowlist (read + write).

Every read of the allowlist goes through here so there is exactly one place that knows the table's
shape. Two capabilities live on one row via two flags:
  • can_watch — a change to this field may fire a Field-Changed rule (grain-independent).
  • can_set   — this field may be written by a Set Field / child-row action (grain-scoped).

Grain is a SET scope only (a can_watch row is validated to have blank grain). So the watch reads ignore
grain; the set reads honour it via `rules.grain_matches` — the same predicate rule-selection uses (one
grain brain). This module replaces the two copies of `_allowlisted` that lived in the dispatcher and the
rule controller.
"""
import frappe

DOCTYPE = "CRM Automation Field"


# -- watch side (grain-independent) ------------------------------------------


def is_watchable(doctype, fieldname):
	"""True if an enabled can_watch row exists for this parent field."""
	return bool(
		frappe.db.exists(
			DOCTYPE, {"doctype_name": doctype, "fieldname": fieldname, "can_watch": 1, "enabled": 1}
		)
	)


def watchable_fields(doctype):
	"""The enabled can_watch fieldnames for a doctype (the Field-Changed dispatch cache + validator)."""
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
