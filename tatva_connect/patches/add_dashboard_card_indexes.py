"""Composite indexes on CRM Task for the dashboard's activity cards.

`(creation, status)` covers `tasks_by_status` and `total_tasks`: the date range seeks, and `status` rides
in the index leaf so the GROUP BY never touches the clustered row. `(status, modified)` covers
`completed_tasks` — the existing `(status, due_date)` leads correctly but cannot serve a `modified` range.
The group-by column trails the range deliberately: nothing after a range column can be seeked, so it earns
its place only by making the index covering.

Nothing is added for CRM Lead: `creation` is already indexed by frappe, and a low-cardinality group-by
column (source has 9 distinct values, vertical 7) cannot carry an index of its own.

Idempotent. install-app baselines patches.txt without running it, so this also runs through
schema_setup._STEPS (mirrors add_task_due_state_index)."""

import frappe

_INDEXES = (
	("ix_task_creation_status", ("creation", "status")),
	("ix_task_status_modified", ("status", "modified")),
)


def execute():
	if not frappe.db.table_exists("CRM Task"):
		return
	# `add_index` checks has_index itself and emits ADD INDEX IF NOT EXISTS (mariadb/database.py:420),
	# so it is idempotent twice over and a failure here is a real one worth surfacing.
	for name, columns in _INDEXES:
		frappe.db.add_index("CRM Task", list(columns), name)
