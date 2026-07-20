"""Re-type `Call Webhook` nodes to `Call API`.

W7 renamed the verb, and a node's `node_type` IS its verb — so a graph still carrying `Call Webhook`
names a verb that no longer exists and the run dies when it reaches that node.

Its own patch rather than an edit to `retire_workflow_action_child`: that one has already run, and an
applied patch is dead — editing it fixes nothing on a site that ran it.

Declared end state: no CRM Workflow Node is typed `Call Webhook`. Idempotent; assumes nothing about
what ran before, and no-ops on a fresh site where no such node was ever authored.
"""
import frappe

OLD, NEW = "Call Webhook", "Call API"


def execute():
	if not frappe.db.exists("DocType", "CRM Workflow Node"):
		return
	for name in frappe.get_all("CRM Workflow Node", filters={"node_type": OLD}, pluck="name"):
		frappe.db.set_value("CRM Workflow Node", name, "node_type", NEW)  # authz-ok: tier-c — patch, no user input
