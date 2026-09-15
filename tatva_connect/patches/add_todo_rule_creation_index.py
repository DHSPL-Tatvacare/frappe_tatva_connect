"""Composite (allocated_to, assignment_rule, creation) on ToDo, so the Credit Weighted daily cap is counted inside the index."""

import frappe

_INDEX = "ix_todo_allocated_rule_creation"


def execute():
	try:
		frappe.db.add_index("ToDo", ["allocated_to", "assignment_rule", "creation"], _INDEX)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"assignment: index {_INDEX} failed")
