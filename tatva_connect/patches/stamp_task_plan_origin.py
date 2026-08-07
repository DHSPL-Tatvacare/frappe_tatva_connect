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
the upgrade path this ALWAYS executes before the field exists. It no-ops then and is logged applied — an
applied patch is dead, so this line never completes the work on the path that needs it. Measured on the
2026-08-06 UAT replay: 60 tasks carried a due date and 0 were stamped. The backfill therefore lives in
`tasks/plan_origin.py` and is run again from `after_migrate`, which is the pass that finishes it; this
line stays only so a site whose column somehow predates it is not left waiting. A fresh site baselines
this line without running it and is born correct, so there is no schema_setup twin: no DDL to repair.

Idempotent: writes only rows still holding the default, so a second run touches nothing.
"""

from tatva_connect.tasks.plan_origin import backfill_is_planned


def execute():
	# The work itself lives in tasks/plan_origin.py because this line runs BEFORE sync_fixtures lands the
	# column, no-ops, and is logged applied — dead for ever. The after_migrate pass completes it.
	backfill_is_planned()
