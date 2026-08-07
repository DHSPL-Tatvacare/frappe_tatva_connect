# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Complete the `custom_is_planned` stamp for every CRM Task written before the stamp existed.

A task row is born an APPOINTMENT (someone promised it) or a RECORD (someone logged what they did), and
the stamp decides that once at insert (`list_engine/columns.py:164`). Every row written before the column
existed carries the `0` default, so the whole history reads as a record and a planned task opens with its
scheduling half missing.

Why this lives here and not only in `patches/stamp_task_plan_origin.py`: the column ships in
`fixtures/custom_field.json`, and `sync_fixtures` runs AFTER post-model-sync patches
(frappe/migrate.py:143-171). On the upgrade path the patch therefore runs before its own column exists,
no-ops on the `has_column` guard, and is logged applied — dead for ever, with the backfill never done.
Measured on the 2026-08-06 UAT replay: 60 tasks carried a due date and 0 were stamped. So the patch is
skip-until-ready and this same function runs in `after_migrate`, the pass that completes it. Exactly the
contract `activity.backfill.ensure_section_rows` already ships under.

ONE-SHOT, and that is load-bearing rather than an optimisation. The only evidence left for a row already
in the table is the due date it holds NOW — which is precisely the read-time derivation the stamp exists
to stop trusting. It is sound once, over rows written before the stamp went live, and wrong every time
after: a task legitimately born a record that later gains a due date would be re-read as planned on the
next migrate and silently flipped. So the pass records that it has run and never judges a row again.
"""
import frappe

# Written to frappe's own defaults store the first time the pass completes. No new doctype and no DDL —
# the marker has to outlive the transaction and be readable before any row is touched, which is all
# `__default` is for.
_MARK = "tatva_connect:is_planned_backfilled"


def backfill_is_planned():
	"""Stamp `custom_is_planned=1` on every pre-stamp task that carries a due date. Returns the row count.

	No-ops when the column has not landed yet (the patch's path) and when the pass has already completed
	(every migrate after the first)."""
	if not frappe.db.has_column("CRM Task", "custom_is_planned"):
		return 0
	if frappe.db.get_default(_MARK):
		return 0

	task = frappe.qb.DocType("CRM Task")
	(
		frappe.qb.update(task)
		.set(task.custom_is_planned, 1)
		.where(task.due_date.isnotnull() & (task.custom_is_planned == 0))
	).run()
	stamped = frappe.db.count("CRM Task", {"custom_is_planned": 1})

	# Set AFTER the write, so a failure part-way leaves the pass un-marked and the next migrate retries it.
	frappe.db.set_default(_MARK, frappe.utils.now())
	return stamped
