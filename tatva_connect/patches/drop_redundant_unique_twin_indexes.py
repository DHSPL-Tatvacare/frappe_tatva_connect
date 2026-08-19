"""Drops the redundant single-column index sitting beside a UNIQUE constraint on the same column, on the
two biggest tables in the app.

WHAT IS THERE NOW. `custom_lsq_prospect_id` on CRM Lead and `custom_provider_call_id` on CRM Call Log
each ship `unique: 1` AND `search_index: 1`, so MariaDB holds TWO indexes on the one column: the unique
constraint, and a second non-unique index the search_index flag built. Observed on prod:

    tabCRM Lead      custom_lsq_prospect_id (unique) + custom_lsq_prospect_id_index (non-unique)
    tabCRM Call Log  custom_provider_call_id (unique) + custom_provider_call_id_index (non-unique)

WHY THE SECOND ONE BUYS NOTHING. A unique index IS an index. Every read the non-unique twin can serve —
equality, range, ordering, covering — the unique one serves identically, and it leads on the same single
column so there is no prefix the twin reaches first. What the twin does cost is a write on every insert
and update of the two largest tables in the app (172,923 leads, 249,997 call logs) and a second plan for
the optimiser to choose wrongly between, which is the failure `drop_step_log_contact_index` already
recorded once on CRM Workflow Step Log.

WHY THE FLAG AND NOT A DROP INDEX. Dropping by name is undone by the next migrate: with `search_index: 1`
still on the Custom Field, `database/schema.py:314` sees a declared index missing and rebuilds it. Setting
the flag to 0 makes frappe's own sync drop it — schema.py:311 — and keeps it dropped. Verified on a bench
before writing this: setting search_index 0 removed `custom_lsq_prospect_id_index` and left the UNIQUE
`custom_lsq_prospect_id` untouched, because the drop path asks `get_column_index(..., unique=False)` and
never matches a unique index.

The flag is cleared in fixtures/custom_field.json in this same change. Without that, sync_fixtures writes
`search_index: 1` back on the next migrate and this patch un-does itself for ever — the exact trap
`drop_step_log_contact_index` documents.

`unique: 1` IS UNTOUCHED on both. The constraint is the point: it is what stops a second lead being
created for one LSQ prospect and a second call log for one provider call id. Only the redundant copy of
the index goes.

`updatedb` is called here because sync_fixtures runs AFTER the schema sync (migrate.py:148), so a
fixture-only change would not take effect until the following migrate.

Idempotent (skips a field already at 0, and updatedb is a no-op once the index is gone). No
schema_setup twin: a fresh site builds both tables from a fixture that no longer declares the flag, so
the redundant index is never created in the first place.
"""
import frappe

_FIELDS = (("CRM Lead", "custom_lsq_prospect_id"), ("CRM Call Log", "custom_provider_call_id"))


def execute():
	touched = set()
	for doctype, fieldname in _FIELDS:
		if not frappe.db.table_exists(doctype):
			continue
		name = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": fieldname})
		if not name or not frappe.db.get_value("Custom Field", name, "search_index"):
			continue
		frappe.db.set_value("Custom Field", name, "search_index", 0)
		touched.add(doctype)
	for doctype in sorted(touched):
		frappe.clear_cache(doctype=doctype)
		frappe.db.updatedb(doctype)
