# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 3 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md: the history moves too.

Declares the end state that every answer a CRM Task already carries in a slot column or in the JSON
payload also sits at the address `field_target` names. The work itself lives in `activity/backfill.py`
because it depends on a fixture Table field and on the `after_migrate` section seed — both of which land
AFTER post-model-sync patches (frappe/migrate.py:139-202). On a site already carrying Phases 1-2 this
line does the backfill; on the upgrade that carries the whole chain in one migrate it is skip-until-ready
and the `after_migrate` pass completes it. Idempotent either way, and it assumes nothing about which of
the two ran.
"""
from tatva_connect.activity import backfill


def execute():
	backfill.ensure_section_rows()
