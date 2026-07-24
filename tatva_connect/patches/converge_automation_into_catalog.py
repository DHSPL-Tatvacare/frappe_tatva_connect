"""Converge the automation field allowlist into the resource brains (one brain per resource).

`CRM Automation Field` was a parallel catalog of which fields exist / where they live / which grain. Its
three capabilities (can_read / can_watch / can_set) fold onto the resource catalog they describe — lead
fields onto `CRM Lead API Field`, activity fields onto `CRM Task Type Field`. Routing now derives from
`CRM Lead Section` and grain from the internal contract, so those columns are simply dropped with the
doctype. The old master gate `enabled` folds INTO the capability: a disabled row grants nothing.

Ships EMPTY in most sites (operator-curated; 0 rows on a clean install), so the copy is a no-op there; it
exists to carry any dev/staging rows and to retire the doctype cleanly. IDEMPOTENT — guarded by the
TABLE's existence (read via raw SQL, so it survives orphan-doctype removal); safe to run twice; a fresh
install never had the doctype (JSON archived), so it no-ops.
"""
import frappe

_OLD = "CRM Automation Field"
_LEAD_DT = "CRM Lead"
_TASK_DT = "CRM Task"


def execute():
	if not frappe.db.table_exists(_OLD):
		return  # already converged, or fresh install (doctype JSON archived) — nothing to carry.

	# Raw read: works whether or not the DocType meta still exists (orphan removal may have run first).
	rows = frappe.db.sql(
		"SELECT doctype_name, fieldname, enabled, can_read, can_watch, can_set, child_table_field"
		" FROM `tabCRM Automation Field`",
		as_dict=True,
	)

	carried, unmapped = 0, []
	for r in rows:
		# enabled is the master gate; a disabled row grants nothing. can_watch implies can_read.
		on = bool(r.get("enabled"))
		caps = {
			"can_read": int(on and bool(r.get("can_read") or r.get("can_watch"))),
			"can_watch": int(on and bool(r.get("can_watch"))),
			"can_set": int(on and bool(r.get("can_set"))),
		}
		targets = _catalog_targets(r)
		if not targets:
			unmapped.append(f"{r.get('doctype_name')}.{r.get('fieldname')}")
			continue
		for catalog, name in targets:
			cur = frappe.db.get_value(catalog, name, ["can_read", "can_watch", "can_set"], as_dict=True) or {}
			# OR the capability in — a field may have carried more than one automation row.
			frappe.db.set_value(
				catalog,
				name,
				{
					"can_read": caps["can_read"] or (cur.get("can_read") or 0),
					"can_watch": caps["can_watch"] or (cur.get("can_watch") or 0),
					"can_set": caps["can_set"] or (cur.get("can_set") or 0),
				},
			)
			carried += 1

	if unmapped:
		# File / WhatsApp Message and any other subject have no catalog yet — fold them as they arise. Never
		# silently drop live allowlist data: leave a trace so the fold can pick them up in a later resource.
		frappe.log_error(
			title="converge_automation_into_catalog: unmapped rows",
			message="No resource catalog for these automation fields:\n" + "\n".join(sorted(set(unmapped))),
		)

	# Drop the doctype + its table (delete_doc goes through the framework's own DDL, not a raw ALTER, so the
	# column-cache guard the DDL lock protects is honoured). If only an orphan table remains, drop that.
	if frappe.db.exists("DocType", _OLD):
		frappe.delete_doc("DocType", _OLD, force=1, ignore_missing=True)
	# delete_doc does NOT reliably drop the physical table inside a migrate (the DocType doc goes but the
	# table lingers as an orphan) — so always ensure the table itself is gone, through the one DDL door.
	if frappe.db.table_exists(_OLD):
		from tatva_connect.patches import _schema

		_schema.ddl("DROP TABLE IF EXISTS `tabCRM Automation Field`", _OLD)
	frappe.db.commit()


def _catalog_targets(r):
	"""The catalog rows a CRM Automation Field row's capabilities land on. Lead fields → CRM Lead API Field
	rows with the same fieldname whose section routing matches the row's child_table_field; activity fields
	→ CRM Task Type Field rows with the same fieldname."""
	fieldname = r.get("fieldname")
	if r.get("doctype_name") == _LEAD_DT:
		out = []
		for lf in frappe.get_all("CRM Lead API Field", filters={"fieldname": fieldname}, fields=["name", "section"]):
			if not lf.section:
				continue  # a row with no section has no routing to compare; get_cached_doc on a blank name raises DoesNotExistError and aborts the migrate
			sec = frappe.get_cached_doc("CRM Lead Section", lf.section)
			if (sec.child_table_field or "") == (r.get("child_table_field") or ""):
				out.append(("CRM Lead API Field", lf.name))
		return out
	if r.get("doctype_name") == _TASK_DT:
		return [("CRM Task Type Field", tf) for tf in frappe.get_all("CRM Task Type Field", filters={"fieldname": fieldname}, pluck="name")]
	return []
