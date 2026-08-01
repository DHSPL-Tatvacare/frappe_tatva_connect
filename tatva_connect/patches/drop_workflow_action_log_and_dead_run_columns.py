"""W12 — one history table, not two, and no column left over from the Journey rename.

`CRM Workflow Action Log` was clearly meant to hold what a node DID to WHOM. It never got a writer: zero
rows, and four of its six columns duplicate `CRM Workflow Step Log`, which IS written and IS what
`history.py` reads. Two tables for one story is how a reader ends up asking which one is true, so the
dead one goes and Step Log gained `channel`/`contact` instead.

DELETING THE DOCTYPE DOES NOT DROP THE TABLE. `delete_from_table` (`delete_doc.py:247`) removes the
DocType ROW and its DocFields and stops there, so assuming otherwise leaves an orphan `tab` table behind
— measured, after the first run of this patch did exactly that. The table is dropped explicitly, BEFORE
the DocType row, because a live DocType over a missing table throws on every read of it.

`workflow_run` is dead on BOTH log tables. W5's rename went through `rename_field`, which COPIES the
column and leaves the original behind; the drop was deferred and then forgotten. Neither doctype JSON
declares it, so nothing reads it and a fresh site never has it — which is also why this needs no
`schema_setup._STEPS` twin: there is nothing to drop on a site born after the rename.

Idempotent both ways. DDL goes through `patches/_schema.ddl`, the ONE door, so frappe's cached column
list is busted immediately — a raw ALTER leaves `has_column()` reading a pre-DDL lie for the rest of the
migrate, which is what killed a deploy once.
"""

import frappe

from tatva_connect.patches import _schema

_DEAD_DOCTYPE = "CRM Workflow Action Log"
_DEAD_COLUMN = "workflow_run"
_TABLES = ("CRM Workflow Step Log", "CRM Workflow Action Log")


def execute():
	for doctype in _TABLES:
		if not frappe.db.table_exists(doctype):
			continue
		table = f"tab{doctype}"
		if frappe.db.has_column(doctype, _DEAD_COLUMN):
			_schema.ddl(f"ALTER TABLE `{table}` DROP COLUMN `{_DEAD_COLUMN}`", table)

	if frappe.db.table_exists(_DEAD_DOCTYPE):
		_schema.ddl(f"DROP TABLE `tab{_DEAD_DOCTYPE}`", f"tab{_DEAD_DOCTYPE}")

	if frappe.db.exists("DocType", _DEAD_DOCTYPE):
		# `force` because the DocType is being retired outright; there are no rows and nothing links to it.
		frappe.delete_doc("DocType", _DEAD_DOCTYPE, force=True, ignore_permissions=True)  # authz-ok: tier-a — patch, retiring a dead doctype
