"""Composite (allocated_to, reference_type, status) on ToDo — the other half of the lead permission
predicate, indexed before the assignment migration makes it expensive.

WHAT READS IT. `crm/permissions/org_hierarchy.py:42-50` answers "leads assigned to me, rather than
owned by me" with `name IN (SELECT reference_name FROM tabToDo WHERE reference_type = 'CRM Lead' AND
status != 'Cancelled' AND allocated_to = me)`. That subquery runs on every Leads and Deals list read,
and — through access/visibility.py's ViaParent — nested one level deeper on every CRM Task, CRM Call
Log, FCRM Note, WhatsApp Message and Workflow Journey/Signal list read too.

WHY THE EXISTING INDEX CANNOT SERVE IT. ToDo carries `reference_type_reference_name_index
(reference_type, reference_name)`. This predicate never names `reference_name`, so only the leading
column is usable and `reference_type` has a handful of distinct values across the table — the read
degrades to "every ToDo of type CRM Lead, filtered in memory". `allocated_to` leads here because it is
the selective column; `reference_type` and `status` ride in the leaf so the whole predicate is answered
inside the index and no row is fetched to test it.

WHY NOW, WHILE IT IS FREE. tabToDo holds 31 rows on prod today, so this costs nothing and proves
nothing — an index on 31 rows is a formality. The assignment migration is underway and reps start on
20 Aug 2026; every assigned lead writes a ToDo, so the table goes to roughly one row per assigned lead
(~173k) within days. At that point this subquery becomes a SECOND table-sized scan stacked on the lead
scan, on every list page load. Adding the index while the table is empty is instant; adding it after is
an ALTER on a live hot table during the first week reps are using the system.

Composite, so a doctype JSON declaration cannot express it, and frappe-owned besides — ToDo belongs to
frappe, so its JSON is not ours to edit. Being composite is also what makes it durable: schema.py's
drop path only ever removes single-column indexes (get_column_index returns nothing for an index with a
second column), so no later sync can take this away.

Idempotent (has_index guard). Also in schema_setup._STEPS.
"""
import frappe

_TABLE = "tabToDo"
_INDEX = "ix_todo_allocated_ref_status"
_COLUMNS = ("allocated_to", "reference_type", "status")


def execute():
	if not frappe.db.table_exists("ToDo"):
		return
	if frappe.db.has_index(_TABLE, _INDEX):
		return
	try:
		frappe.db.add_index("ToDo", list(_COLUMNS), _INDEX)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"visibility: index {_INDEX} failed")
