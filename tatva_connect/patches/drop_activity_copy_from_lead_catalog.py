# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Delete the activity schema's copy from the lead catalog, and the two columns that held it there.

`CRM Lead API Field` carried 397 rows that were not lead fields: a frozen copy of every activity type's
schema, generated at seed time by a pure function of `CRM Task Type Field`. It was addressed by
`applies_to` — a Data field holding a task type's name as TEXT. `rekey_task_types_composite` then
re-keyed every type to its composite grain; every real Link cascaded and the text did not. Measured
before this patch: 45 scopes in the copy, 59 buildable from the brain, INTERSECTION 0 — every Activity
Smart View resolved ZERO fields. Smart Views asks the brain now, so there is nothing left to copy.

`sql_source` goes with them. Once every row is a lead field it is a pure function of the row's section
(child_table_field set -> child, else parent), and it was the column that proved the point: added to an
already-populated table, so 29 rows were born blank and are blank still.

End state: every row in this table is a lead field, and neither column exists. Idempotent; assumes
nothing about what ran before.
"""
import frappe

from tatva_connect.patches import _schema

DT = "CRM Lead API Field"
TABLE = "tabCRM Lead API Field"

# The copy's rows are the ones that are not lead fields. `section` is the Link every lead row carries
# (rekey_lead_catalog_sections); a row that resolves to no section has no table, no target and no row
# key — which is precisely what a CRM Task row was doing in the lead catalog.
_LEAD_FIELD_LINK = "section"


def execute():
	_delete_the_copy()
	_drop_the_copys_columns()
	frappe.db.commit()


def _delete_the_copy():
	"""Every row that names no lead section. delete_doc, not a DELETE: the rows are Links' targets
	(CRM Lead API Mapping Field, CRM Lead Field Restriction) and the framework clears them."""
	sections = set(frappe.get_all("CRM Lead Section", pluck="name"))
	for r in frappe.get_all(DT, fields=["name", _LEAD_FIELD_LINK]):
		if r.get(_LEAD_FIELD_LINK) in sections:
			continue
		frappe.delete_doc(DT, r.name, force=True, ignore_permissions=True)  # authz-ok: tier-c — post patch, no session user


def _drop_the_copys_columns():
	"""`applies_to` addressed the copy and `sql_source` routed it. Frappe never drops a column a JSON
	stopped declaring (T10), so the delete is declared here."""
	_schema.refresh(TABLE)
	for column in ("applies_to", "sql_source"):
		if frappe.db.has_column(DT, column):
			_schema.ddl(f"ALTER TABLE `{TABLE}` DROP COLUMN `{column}`", TABLE)
