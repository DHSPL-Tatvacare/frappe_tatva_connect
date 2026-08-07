"""Backfill `custom_is_planned` on every CRM Task that predates the stamp.

A task row is born an APPOINTMENT (someone promised it) or a RECORD (someone logged what they did), and
`TatvaCRMTask.before_insert` now decides that once, from the due date the row arrives with. Every row
written before that stamp existed carries the column's `0` default, so the whole history reads as a
record and a planned task would open with its scheduling half missing.

The only evidence left for a row already in the table is the due date it holds NOW — which is exactly the
read-time derivation the stamp exists to stop trusting. It is sound here and nowhere else: this runs once,
over rows nobody can edit between the ALTER and this line, so "has a due date" and "was born with one"
cannot yet have diverged. Going forward the stamp is authoritative and this reasoning must not be reused.

Migrated LSQ data splits on the same evidence and needs no special case: an LSQ Task carries a schedule
(its Schedule field is required) and an LSQ Activity carries none, so tasks land planned and activities
land as records.

Skip-until-ready: the column arrives with `sync_fixtures`, which runs AFTER post-model-sync patches, so on
the upgrade path this can execute before the field exists. It no-ops then, and the `after_migrate` pass is
not needed to finish it — the same migrate's later run of this patch name never happens, so the guard is
what makes a re-run safe rather than what completes the work. A fresh site baselines this line without
running it and is born correct, so there is no schema_setup twin: no DDL, and nothing to repair.

Idempotent: writes only rows still holding the default, so a second run touches nothing.
"""

import frappe


def execute():
	if not frappe.db.has_column("CRM Task", "custom_is_planned"):
		return

	task = frappe.qb.DocType("CRM Task")
	(
		frappe.qb.update(task)
		.set(task.custom_is_planned, 1)
		.where(task.due_date.isnotnull() & (task.custom_is_planned == 0))
	).run()
