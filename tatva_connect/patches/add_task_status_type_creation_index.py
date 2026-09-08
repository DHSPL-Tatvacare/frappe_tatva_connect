"""(status, custom_task_type, creation) on CRM Task — the TatvaPractice Field Visit Review report's own
WHERE clause, measured against prod 2026-09-08: `EXPLAIN` showed `type: ALL`, a full scan of all 665,542
rows, even though `creation`, `custom_task_type_index` and `ix_task_creation_status` all exist. None of
them cover the actual shape this query asks — equality on `status`, a prefix range on `custom_task_type`
('Tatvapractice::India::Field-Sales::%'), and a range on `creation` — so the optimiser priced a scan
below an index seek that would still need a row lookup per match. `status` leads because it is the
cheapest equality; `custom_task_type` second because the vertical prefix is far more selective than the
date window alone (this table holds every business line, not just TatvaPractice); `creation` last as the
range column, following the same "equality columns before the range column" shape as `ix_refdoc_tasktype_status`
already on this table. Declares the end state, no-op twice; install-app baselines patches.txt without
running it, so schema_setup._STEPS carries this to a fresh site too."""
import frappe

from tatva_connect.patches import _schema

_DOCTYPE = "CRM Task"
_TABLE = "tabCRM Task"
_INDEX = ("ix_task_status_type_creation", "`status`, `custom_task_type`, `creation`")


def execute():
	if not frappe.db.table_exists(_DOCTYPE):
		return
	if not frappe.db.has_column(_DOCTYPE, "custom_task_type"):
		return  # the doctype JSON has not synced yet; the after_migrate schema_setup pass lands it
	name, columns = _INDEX
	if not frappe.db.has_index(_TABLE, name):
		_schema.ddl(f"ALTER TABLE `{_TABLE}` ADD INDEX `{name}` ({columns})", _TABLE)  # sqli-ok: constant identifiers only, no user value reaches this string
