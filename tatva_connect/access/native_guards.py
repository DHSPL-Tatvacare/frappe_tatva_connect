# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Authorization gates over native crm whitelisted methods that BYPASS the permission engine
(get_all / ignore_permissions / un-gated get_doc), so the doctype matrix can never reach them.

Each wrapper asserts the caller may READ the target, then delegates to the UNCHANGED native
function. Wired via override_whitelisted_methods (hooks.py): the crm endpoint runs our gate
first; the native code runs verbatim once the gate passes. No crm fork.

The native function is imported DIRECTLY (not dispatched), so it never re-enters the override —
no recursion. Contract mirrors §5 of the existing brain work: visibility always checks READ;
the action level is the doctype's own role docperm.
"""
import frappe


@frappe.whitelist()
def get_assigned_users(doctype, name, default_assigned_to=None):
	frappe.has_permission(doctype, "read", name, throw=True)
	from crm.api.doc import get_assigned_users as _native

	return _native(doctype, name, default_assigned_to)


@frappe.whitelist()
def get_linked_docs_of_document(doctype, docname):
	frappe.has_permission(doctype, "read", docname, throw=True)
	from crm.api.doc import get_linked_docs_of_document as _native

	return _native(doctype, docname)


@frappe.whitelist()
def add_task_to_call_log(call_sid, task):
	# you may attach a task only to a call log you can READ (its parent Lead/Deal, via the brain)
	frappe.has_permission("CRM Call Log", "read", call_sid, throw=True)
	from crm.integrations.api import add_task_to_call_log as _native

	return _native(call_sid, task)
