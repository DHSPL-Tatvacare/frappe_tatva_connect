"""`search_index` on CRM Lead.lead_owner — the one column every non-privileged read filters on and the
only table-sized scan left in the app's hot path.

WHAT IT COSTS TODAY. `_permission_query_conditions` (crm/permissions/org_hierarchy.py:54) narrows every
Sales User to `lead_owner = me OR name IN (<ToDo subquery>)`, and an in-tree user to the same plus their
subtree. `lead_owner` carries no index, so MariaDB cannot seek it: it walks the `modified` index and
tests each row. Measured on prod (172,923 leads) for a rep who owns ONE lead: 169,733 rows read,
509,605 pages, 352 ms, r_filtered 0.0006% — to return that one lead. The less a rep owns the slower
their list, because the optimiser walks further looking for matches it never finds.

IT IS NOT ONLY THE LEADS LIST. `access/visibility.py:108` wraps the same predicate as a subquery for
every doctype scoped ViaParent — CRM Task, CRM Call Log, FCRM Note, WhatsApp Message, CRM Workflow
Journey and CRM Workflow Signal. The task list for the same rep measured 1,155 ms: 199,792 task rows
plus a full scan of all 169,733 leads, because the subquery reports `access_type: ALL`. One missing
index, six list surfaces. Server-wide the symptom is visible in the counters: 390M rows read by
sequential scan against 51M by index lookup.

WHY A PROPERTY SETTER AND NOT `add_index`. A single-column index added by DDL does not survive.
`database/schema.py:311` drops any index on a column whose meta does not declare `search_index`, and
`get_column_index` finds it BY COLUMN, not by name — so a hand-named `ix_lead_owner` would be found and
dropped by the next schema sync. (Composite indexes are exempt: that function returns only indexes with
no second column, which is why every other index patch in this app is composite.) Declaring the flag in
meta makes the sync the thing that MAINTAINS the index instead of the thing that deletes it. frappe
does this itself for single-field indexes in `add_index` (database.py:428) but skips it during a
migrate, which is exactly when a patch runs.

`lead_owner` is a crm-owned standard field, so a Property Setter is the only place the flag can live;
the twin row in fixtures/property_setter.json is what stops a later sync_fixtures from reverting it.

WHY `updatedb` IS CALLED HERE. sync_fixtures runs in post_schema_updates, AFTER the schema sync
(migrate.py:148), so a flag that only arrived by fixture would not become an index until the NEXT
migrate — a silent no-op on the migrate that ships it. This applies the flag and syncs the table in one
pass so prod gets the index on the deploy that carries this line.

Selectivity is right for an index: 128 distinct owners over 172,923 rows, ~0.8% per owner. The ALTER is
`ALGORITHM=INPLACE, LOCK=NONE` for a secondary index, so reads and writes continue while it builds.

Idempotent (make_property_setter upserts; updatedb is a no-op once the index exists). Also in
schema_setup._STEPS.
"""
import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

_DOCTYPE = "CRM Lead"
_FIELD = "lead_owner"


def execute():
	if not frappe.db.table_exists(_DOCTYPE):
		return
	meta = frappe.get_meta(_DOCTYPE)
	if not meta.get_field(_FIELD):
		return  # the field left crm; nothing to index and nothing to repair
	make_property_setter(
		_DOCTYPE, _FIELD, "search_index", 1, "Check", validate_fields_for_doctype=False
	)
	frappe.clear_cache(doctype=_DOCTYPE)
	frappe.db.updatedb(_DOCTYPE)
