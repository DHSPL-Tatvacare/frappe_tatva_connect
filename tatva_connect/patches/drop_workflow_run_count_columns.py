"""Drop `journeys_started` and `last_journey_at` from `tabCRM Workflow`: frozen since `ee4683d`, their fields are gone, and run counts now live on Run history. Frappe never drops a column when a field leaves a doctype, so this does, through `_schema.ddl`. Idempotent (has_column guard). No schema_setup twin: a fresh site never declares them."""

import frappe

from tatva_connect.patches import _schema

DOCTYPE = "CRM Workflow"
_COLUMNS = ("journeys_started", "last_journey_at")


def execute():
	table = f"tab{DOCTYPE}"
	_schema.refresh(table)
	for column in _COLUMNS:
		if frappe.db.has_column(DOCTYPE, column):
			_schema.ddl(f"ALTER TABLE `{table}` DROP COLUMN `{column}`", table)
	frappe.clear_cache(doctype=DOCTYPE)
