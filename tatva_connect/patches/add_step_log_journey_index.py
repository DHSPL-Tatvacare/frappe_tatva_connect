"""Composite index (journey, creation, name) on CRM Workflow Step Log — the exact triple every read of a
run's log seeks on. The table is indexed on `creation` and on `contact` and on NOTHING ELSE, so
`journey_steps` (filter journey, order by creation then name) and the run cards' step totals both scan the
whole table: every step ever logged, for every lead, to answer about one run. Dev already holds 3.8k rows
against a handful of leads; the engine writes one row per node per journey, so in production this is the
table that grows fastest and the scan gets worse every day.

`creation, name` ride in the leaf because that is the EXECUTION order the reader wants — a whole segment is
written inside one transaction and shares a timestamp, so `name` is what breaks the tie (history.py says the
same). With them in the index the log is a seek plus an ordered range and the sort disappears; with only
`journey` it would be a seek and then a filesort of the run's rows.

Doctype JSON cannot express a composite index. Idempotent (has_index guard). install-app baselines
patches.txt without running it, so this also runs on after_migrate via schema_setup (mirrors
add_step_log_contact_index, on this same table)."""

import frappe

_TABLE = "tabCRM Workflow Step Log"
_INDEXES = (("ix_journey_creation", ("journey", "creation", "name")),)


def execute():
	if not frappe.db.table_exists("CRM Workflow Step Log"):
		return
	for name, columns in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index("CRM Workflow Step Log", list(columns), name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"workflow_engine: index {name} failed")
