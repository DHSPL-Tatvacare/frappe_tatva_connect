"""Composite index (workflow_version, creation, node_id) on CRM Workflow Step Log: the canvas traffic count seeks one version, ranges a time window and groups by node inside the index."""

import frappe

_TABLE = "tabCRM Workflow Step Log"
_INDEXES = (("ix_version_creation_node", ("workflow_version", "creation", "node_id")),)


def execute():
	if not frappe.db.table_exists("CRM Workflow Step Log"):
		return
	for name, columns in _INDEXES:
		if frappe.db.has_index(_TABLE, name):
			continue
		try:
			frappe.db.add_index("CRM Workflow Step Log", list(columns), name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"workflow_engine: index {name} failed")
