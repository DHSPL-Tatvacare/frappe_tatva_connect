"""Composite index (custom_task_type, modified) on CRM Task — an Activity Smart View pins the type and pages by `modified`.

Before it the page was `custom_task_type_index` + filesort over every task of the type; with it the type seeks and the
sort is walked. Composite, so not JSON-declarable; idempotent (has_index guard); also in schema_setup._STEPS because
install-app baselines patches.txt without running it (mirrors add_lead_grain_index).
"""

import frappe

_DOCTYPE = "CRM Task"
_TABLE = "tabCRM Task"
_NAME = "ix_task_type_modified"
_COLUMNS = ("custom_task_type", "modified")


def execute():
	if not frappe.db.table_exists(_DOCTYPE):
		return
	if frappe.db.has_index(_TABLE, _NAME):
		return
	if any(not frappe.db.has_column(_DOCTYPE, c) for c in _COLUMNS):
		return
	try:
		frappe.db.add_index(_DOCTYPE, list(_COLUMNS), _NAME)
	except Exception:
		frappe.log_error(title=f"smartview: index {_NAME} failed")
