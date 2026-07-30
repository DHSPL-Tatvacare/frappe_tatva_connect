"""W5.1 — `CRM Workflow Run` becomes `CRM Workflow Journey`.

Declared end state: the doctype, its table and every Link field pointing at it name the Journey.

WHY [pre_model_sync] AND NOT post. The doctype folder is renamed in source, so the model sync would
CREATE `CRM Workflow Journey` as a brand-new empty doctype and table and leave every existing journey
stranded in `tabCRM Workflow Run`. Renaming first means sync has an existing doctype to apply the JSON to.
This is frappe's own placement for the same operation — `rename_custom_client_script` and
`rename_desk_page_to_workspace` both sit in `[pre_model_sync]` in `frappe/patches.txt`.

THE DOOR, AND WHAT WAS REJECTED. `frappe.rename_doc("DocType", old, new)` — the native door frappe's own
patches use. It renames the row, renames the table, and walks `update_options_for_fieldtype` so every
`Link`/`Table` field whose `options` named the old doctype is repointed. Rejected: `patches/_schema.ddl`
with a `RENAME TABLE` (it moves the table and leaves `tabDocType`, every DocField `options`, and the
child-row `parenttype` values naming a doctype that no longer exists) and `_schema.rename_column` (wrong
grain entirely — nothing here renames a column).

NO INDEX RENAME IS NEEDED, and that was measured rather than assumed. `SHOW INDEX` on the live table
returns `PRIMARY, active_key, awaiting_signal, creation, modified, resume_at, status, subject_name,
workflow, workflow_version` — every one named after its COLUMN, because frappe builds them from the
doctype JSON's `unique`/`search_index` flags. No index name embeds the table name, and `RENAME TABLE`
carries indexes across. `patches/add_workflow_run_active_key_unique.py` is applied and therefore dead,
but it never created an index either: its own docstring says the unique index comes from the JSON during
model sync and that patch only made the DATA satisfy it.

A fresh site baselines this line without running it and is born with the new name.

If `CRM Workflow Journey` somehow already exists, `rename_doc` raises rather than merging. That is the
right outcome: two tables that both look like journeys is not a state a patch may guess its way out of.
"""
import frappe

OLD, NEW = "CRM Workflow Run", "CRM Workflow Journey"


def execute():
	if not frappe.db.exists("DocType", OLD):
		return
	frappe.rename_doc("DocType", OLD, NEW, show_alert=False)
	frappe.reload_doctype(NEW)
