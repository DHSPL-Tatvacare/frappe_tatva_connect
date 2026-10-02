"""Drop five single-column indexes the `search_index` flag built and nothing reads.

THE LSQ KEYS (four of the five). `custom_lsq_task_id` and `custom_lsq_activity_id` on CRM Task,
`custom_lsq_activity_id` on FCRM Note and `custom_lsq_attachment_id` on File are the LeadSquared
migration's idempotency keys. They have NO query site in runtime code: the only `custom_lsq_activity_id`
mentions in hooks.py are the fixture-export filter that ships the field and the comment above it, and
`access/internal_contract_seed` lists field NAMES for the entitlement contract, not lookups. Measured on
prod 2026-10-01, `custom_lsq_activity_id_index` on CRM Task reports CARDINALITY 1 over 604,548 rows — one
distinct value, so no query could ever be narrowed by it and no optimiser could ever choose it. CRM Lead's
own lsq columns already carry `search_index: 0`; `custom_lsq_prospect_id` is `unique: 1`, a dedup
constraint rather than a search index, and is deliberately untouched.

THE COLUMNS AND THEIR DATA STAY. This drops indexes, never fields — the LeadSquared audit trail is
unaffected, and a future reconciliation can still read the ids, it just scans to do it.

CRM TASK'S `custom_task_type` (the fifth). `add_activity_view_index` builds
`ix_task_type_modified (custom_task_type, modified)`, and by leftmost prefix that composite serves every
read the single-column `custom_task_type_index` can, better: the Activity Smart View's own query filters
the type and pages by `modified` inside the index instead of leaving it to sort. The narrow one is 44.8 MB
of duplicate B-tree maintained on every task write, and with both present the optimiser has a choice it
can get wrong. GUARDED: the drop is skipped unless the composite is really there, so a deploy that
somehow lacks it cannot leave the column bare. (That patch sits earlier in both patches.txt and
schema_setup._STEPS, so in a normal migrate it has already run.)

THROUGH META, NOT DDL. Clearing `search_index` and calling `updatedb` makes frappe's own schema sync
perform the drop (`build_for_alter_table` appends to `drop_index` when an index exists and the flag is
gone) — the same door that would otherwise rebuild it. The twin flags go to fixtures/custom_field.json in
this change, or sync_fixtures restores them and this patch un-does itself for ever (the shape
drop_step_log_contact_index set). `updatedb` is called here because sync_fixtures runs in
post_schema_updates, AFTER the schema sync (migrate.py), so a flag arriving only by fixture would not
become a drop until the NEXT migrate — a silent no-op on the migrate that ships it.

Idempotent: a field whose flag is already clear is skipped, and `updatedb` is a no-op once the index is
gone. Also in schema_setup._STEPS.
"""

import frappe

# (doctype, fieldname, index that must already exist for the drop to be safe — None where nothing reads
# the column at all).
_UNFLAG = (
	("CRM Task", "custom_lsq_task_id", None),
	("CRM Task", "custom_lsq_activity_id", None),
	("CRM Task", "custom_task_type", "ix_task_type_modified"),
	("FCRM Note", "custom_lsq_activity_id", None),
	("File", "custom_lsq_attachment_id", None),
)


def execute():
	touched = set()
	for doctype, fieldname, covered_by in _UNFLAG:
		if not frappe.db.table_exists(doctype):
			continue
		cf = f"{doctype}-{fieldname}"
		if not frappe.db.exists("Custom Field", cf):
			continue
		if not frappe.db.get_value("Custom Field", cf, "search_index"):
			continue
		# A column something still filters on keeps its index until the composite that LEADS with it is
		# really present — dropping the only one would turn that read into a scan.
		if covered_by and not frappe.db.has_index(f"tab{doctype}", covered_by):
			continue
		frappe.db.set_value("Custom Field", cf, "search_index", 0)
		touched.add(doctype)

	for doctype in sorted(touched):
		frappe.clear_cache(doctype=doctype)
		try:
			frappe.db.updatedb(doctype)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"indexes: drop on {doctype} failed")
