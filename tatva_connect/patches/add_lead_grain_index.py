"""Composite index (custom_vertical, custom_group, custom_current_program, modified) on CRM Lead — the
grain a Smart View narrows on, with the sort column in the leaf.

WHY. A Lead Smart View is scoped by its predicate, and every grain-scoped view states the three axes.
Nothing on the table could serve that: `ix_lead_dedup_unique` leads with `mobile_no`, `lead_owner_index`
with `lead_owner`, so the count fell back to `type: ALL` — a FULL TABLE SCAN of every lead, run on every
page open of every view for every rep, because the row page is capped at PAGE_MAX but the count is not
(smartview/api.py:get_data says exactly this: the count is the unbounded half of the call).

Measured on dev before/after, 15,508 leads, 639 of them Inside-Sales:
  count   ALL / 14,950 rows / no key   ->  ref / 639 rows / Using index      13.3ms -> 2.1ms
  rows    index scan + filter          ->  ref / Using index, no filesort
The gain is not the 6x; it is that the plan stops being O(table) and becomes O(matches), so it does not
degrade as the lead table grows.

COLUMN ORDER IS LOAD-BEARING. The three equality columns lead, so the same index serves the WHERE; and
`modified` sits last so the default `ORDER BY modified DESC LIMIT n` is answered by walking the index
instead of sorting the matches. Both queries come back `Using index` — the table is never touched.

Composite, so the doctype JSON cannot declare it. Idempotent (has_index guard). install-app baselines
patches.txt WITHOUT running it, so this is also in schema_setup._STEPS (mirrors add_task_due_state_index).
"""

import frappe

# TWO CONVENTIONS, one line apart: `has_index` takes the TABLE (`tabCRM Lead`) while `has_column` and
# `add_index` take the DOCTYPE (`CRM Lead`). Passing the table to has_column raises TableMissingError on
# `tabtabCRM Lead`, outside the try below — the patch then throws on every migrate. Both names are held.
_DOCTYPE = "CRM Lead"
_TABLE = "tabCRM Lead"
_NAME = "ix_lead_grain_modified"
_COLUMNS = ("custom_vertical", "custom_group", "custom_current_program", "modified")


def execute():
	if not frappe.db.table_exists(_DOCTYPE):
		return
	if frappe.db.has_index(_TABLE, _NAME):
		return
	# A column the grain seed has not created yet is not an error on a young site — the index arrives with it.
	if any(not frappe.db.has_column(_DOCTYPE, c) for c in _COLUMNS):
		return
	try:
		frappe.db.add_index(_DOCTYPE, list(_COLUMNS), _NAME)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"smartview: index {_NAME} failed")
