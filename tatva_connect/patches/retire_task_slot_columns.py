# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Retire the six dead CRM Task slot columns — Phase 7 of the task-sections plan (§5.5).

`custom_key_date_1..4` and `custom_reference` are the shared slots whose INDEX carried the meaning
("Assessment Date - Physio" is in key_date_1 only because it was the next free one), and
`custom_activity_payload` is the JSON blob that could never be filtered or sorted. Every answer they held
lives in a `CRM Task Section` row now, addressed by `field_target`; nothing reads any of the six.

DECLARED END STATE: neither the Custom Field doc nor the physical column exists for any of the six. The
fixture no longer declares them, so a FRESH site is born in that state and never runs this at all
(`install-app` baselines patches.txt without executing it) — which is also why the drop is mirrored in
`schema_setup._STEPS` rather than left to this line alone. That mirror is not the index-patch story of
landing structure a fresh site would otherwise miss; it is what lets the drop happen AT ALL on a site that
is not yet backfilled, because of the refusal below.

SKIP-UNTIL-SAFE, and why it must be. A post-model-sync patch runs BEFORE `sync_fixtures` and BEFORE
`after_migrate`, and the pass that copies these answers OUT of the columns
(`activity.backfill.ensure_section_rows`) is an `after_migrate` hook. So on the migrate that first carries
this chain the copy has NOT happened yet, and dropping now would destroy the data permanently. This patch
therefore proves the end state it needs rather than assuming a predecessor ran: it asks the ONE audit
(`activity.backfill.audit`, the same predicate `reconcile` reports from) whether any answer still lives
only in an old home, and REFUSES — loudly, into the Error Log, never a throw that would abort the whole
migrate — while one does. `after_migrate` then completes the copy, and because an applied patch is dead
the drop lands on the next run through `schema_setup._STEPS`, which re-asserts this same end state on
every migrate for ever.

Idempotent: with nothing left to retire it returns before it reads a single task, so a fresh install and a
second run are both free.
"""
import frappe

from tatva_connect.activity import api as activity_api
from tatva_connect.activity import backfill
from tatva_connect.patches import _schema

TASK = "CRM Task"
_TABLE = "tabCRM Task"

# The six §5.5 names. Stated here because a DROP is the one statement that must name what it removes; every
# other consumer resolves through the declaration, and the fixture no longer declares any of them.
_DEAD = (
	"custom_key_date_1", "custom_key_date_2", "custom_key_date_3", "custom_key_date_4",
	"custom_reference", "custom_activity_payload",
)

# How many offenders the refusal names. Enough for an operator to act on; the audit stops there rather than
# scanning every task to count a number nobody needs.
_NAME_AT_MOST = 10


def _column_exists(column):
	"""True if the column physically exists; read from information_schema (has_column can report stale after a raw DDL change in the same migrate)."""
	return bool(
		frappe.db.sql(
			"""SELECT 1 FROM information_schema.COLUMNS
			WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s""",
			(_TABLE, column),
		)
	)


def _still_declared(fieldname):
	"""What is left of this field: its Custom Field doc, its column, or neither (the declared end state)."""
	return frappe.db.exists("Custom Field", f"{TASK}-{fieldname}") or _column_exists(fieldname)


def _retire(fieldname):
	"""Drop one retired field end to end: the Custom Field doc first, then the column — deleting the doc leaves the column behind, and dropping the column first would leave the meta describing a column every read SELECTs."""
	cf = f"{TASK}-{fieldname}"
	if frappe.db.exists("Custom Field", cf):
		frappe.delete_doc("Custom Field", cf, ignore_permissions=True, force=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	if _column_exists(fieldname):
		# sql_ddl through the one door, not a bare ALTER: frappe caches the table's column list and the next guard would read a pre-DDL lie.
		_schema.ddl(f"ALTER TABLE `{_TABLE}` DROP COLUMN `{fieldname}`", _TABLE)


def execute():
	if not frappe.db.table_exists(TASK):
		return
	if not any(_still_declared(f) for f in _DEAD):
		return  # the end state is already true: nothing to delete, nothing to drop, no task to read
	if not activity_api.sections_ready():
		# The copy has not even been POSSIBLE yet — `CRM Task Section` is seeded by section_seed.ensure_rows
		# on after_migrate, which runs after this line. An audit asked now reports nothing unhomed simply
		# because it cannot route anything, and reading that as "safe to drop" would delete every answer
		# still living only in a slot. Refuse; the after_migrate pass copies, and the next migrate drops.
		frappe.log_error(
			title="retire_task_slot_columns: refused, the section declaration is not seeded yet",
			message=(
				"No CRM Task Section is declared key-value, so no answer can have been copied to its new "
				"home and the audit cannot route one. The columns were left in place. "
				"`section_seed.ensure_rows` and `activity.backfill.ensure_section_rows` are after_migrate "
				"hooks, so this migrate seeds and copies, and the drop lands on the run after it."
			),
		)
		return
	_routed, unhomed = backfill.audit(limit=_NAME_AT_MOST)
	if unhomed:
		# LOUD, and not a throw: this runs inside the migrate (and inside schema_setup's after_migrate pass),
		# so raising would fail the deploy over a state the next run fixes by itself.
		frappe.log_error(
			title="retire_task_slot_columns: refused, answers are not homed yet",
			message=(
				"At least one activity answer still lives ONLY in a slot column or the JSON payload, so "
				"dropping them now would lose it. The columns were left in place. Run "
				"`activity.backfill.ensure_section_rows` (it is an after_migrate hook, so the next migrate "
				"does it) and this drop lands on the run after that.\n\n"
				+ "\n".join(f"{task} :: {fieldname}" for task, fieldname in unhomed)
			),
		)
		return
	for fieldname in _DEAD:
		_retire(fieldname)
