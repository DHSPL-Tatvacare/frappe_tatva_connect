"""Retire the partner-silence email's fields: a scheduled_batch Webhook with Send When = No Rows Found replaced it.
CRM Partner API Settings is a Single, so its dead values are `tabSingles` rows a JSON field removal never clears; the
contract's tick is a real column, dropped through the one DDL door. Idempotent, forward-only."""
import frappe

from tatva_connect.patches import _schema

_SINGLE = "CRM Partner API Settings"
_SINGLE_FIELDS = ("silence_alert_hours", "silence_alert_recipients")
_CONTRACT = "CRM Lead API Mapping"
_CONTRACT_FIELD = "alert_on_silence"


def execute():
	for fieldname in _SINGLE_FIELDS:
		frappe.db.delete("Singles", {"doctype": _SINGLE, "field": fieldname})
	frappe.clear_cache(doctype=_SINGLE)
	if frappe.db.has_column(_CONTRACT, _CONTRACT_FIELD):
		_schema.ddl(f"ALTER TABLE `tab{_CONTRACT}` DROP COLUMN `{_CONTRACT_FIELD}`", f"tab{_CONTRACT}")
