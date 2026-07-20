"""Re-type `Assign` nodes to `Set Variables`.

The node computes values into run state and has nothing to do with people. A new `Assign to User` node
now moves ownership of a lead, and two nodes called Assign doing unrelated things would mislead every
author — the one that sounds like it assigns a person would be the one that does not.

A node's `node_type` IS its verb, so a row still typed `Assign` names a type that no longer exists and
the run dies when it reaches it.

Declared end state: no CRM Workflow Node is typed `Assign`. Idempotent; no-op on a fresh site.
"""
import frappe

OLD, NEW = "Assign", "Set Variables"


def execute():
	if not frappe.db.exists("DocType", "CRM Workflow Node"):
		return
	for name in frappe.get_all("CRM Workflow Node", filters={"node_type": OLD}, pluck="name"):
		frappe.db.set_value("CRM Workflow Node", name, "node_type", NEW)  # authz-ok: tier-c — patch, no user input
