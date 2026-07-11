"""Merge the two automation field allowlists into one — `CRM Automatable Field` (write) +
`CRM Automation Watchable Field` (read) → `CRM Automation Field` (can_set / can_watch flags on one row).

Both old tables ship EMPTY in production (operator-curated; LESSONS.md confirms 0 rows on a clean
install), so this is a no-op there; it exists to carry any dev/staging rows and to retire the old
doctypes cleanly. Idempotent — guarded by `frappe.db.exists("DocType", OLD)`; no-op on a fresh install
(the old doctypes' JSON was archived, so they never sync there).

Trace (A.14): the old write table's `label` + `section_key` columns are dropped — they were never read
anywhere in the engine (describe/dispatcher/validator all ignored them). Data type upgraded:
`target_doctype` (Data) → `doctype_name` (Link).
"""
import frappe

_WRITE = "CRM Automatable Field"
_WATCH = "CRM Automation Watchable Field"
_NEW = "CRM Automation Field"


def execute():
	if not (frappe.db.exists("DocType", _WRITE) or frappe.db.exists("DocType", _WATCH)):
		return  # fresh install — the merged doctype is the only one; nothing to carry.

	source, migrated, failures = 0, 0, []

	if frappe.db.exists("DocType", _WRITE):
		rows = frappe.get_all(
			_WRITE,
			fields=["target_doctype", "fieldname", "child_table_field", "is_row_key", "vertical", "group", "program", "enabled"],
		)
		source += len(rows)
		for r in rows:
			if _upsert(
				doctype_name=r.target_doctype, fieldname=r.fieldname,
				child_table_field=r.child_table_field, vertical=r.vertical, group=r.group, program=r.program,
				enabled=r.enabled, can_set=1, is_row_key=r.is_row_key,
			):
				migrated += 1
			else:
				failures.append(f"{_WRITE}: {r.target_doctype}.{r.fieldname}")

	if frappe.db.exists("DocType", _WATCH):
		rows = frappe.get_all(_WATCH, fields=["doctype_name", "fieldname", "enabled"])
		source += len(rows)
		for r in rows:
			if _upsert(doctype_name=r.doctype_name, fieldname=r.fieldname, enabled=r.enabled, can_watch=1):
				migrated += 1
			else:
				failures.append(f"{_WATCH}: {r.doctype_name}.{r.fieldname}")

	# DATA-LOSS GUARD (audit H1): NEVER drop the source unless every row landed. Abort loudly, leaving
	# both source tables intact, so the operator fixes the offending legacy row (a since-removed field,
	# a non-Link target_doctype, a parent is_row_key) and re-runs. A silent skip-then-drop is a trap.
	if migrated != source:
		frappe.throw(
			"merge_automation_field_allowlists: only {}/{} allowlist rows migrated — refusing to drop "
			"the source tables. Fix and re-run: {}".format(migrated, source, "; ".join(failures))
		)

	for old in (_WRITE, _WATCH):
		if frappe.db.exists("DocType", old):
			frappe.delete_doc("DocType", old, ignore_permissions=True, force=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
			frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{old}`")  # force-delete leaves the tab table

	frappe.db.commit()


def _upsert(doctype_name, fieldname, child_table_field=None, vertical=None, group=None, program=None,
            enabled=1, can_watch=0, can_set=0, is_row_key=0):
	"""Insert the merged row, or OR the capability flags onto an existing one (a field both watched and
	set collapses to one row). Returns True on success, False on a row that couldn't be migrated (the
	caller counts these and aborts before the DROP). Uses the DETERMINISTIC composite name — matching
	the doctype's autoname format — so a `""` vs NULL grain mismatch can't miss the collapse target."""
	name = "::".join([
		doctype_name or "", child_table_field or "", fieldname or "",
		vertical or "", group or "", program or "",
	])
	try:
		if frappe.db.exists(_NEW, name):
			doc = frappe.get_doc(_NEW, name)
			doc.can_watch = 1 if (doc.can_watch or can_watch) else 0
			doc.can_set = 1 if (doc.can_set or can_set) else 0
			doc.is_row_key = 1 if (doc.is_row_key or is_row_key) else 0
			doc.save(ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
		else:
			frappe.get_doc({
				"doctype": _NEW, "doctype_name": doctype_name, "fieldname": fieldname,
				"child_table_field": child_table_field or "", "vertical": vertical or "",
				"group": group or "", "program": program or "",
				"can_watch": can_watch, "can_set": can_set, "is_row_key": is_row_key, "enabled": enabled,
			}).insert(ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
		return True
	except Exception:
		frappe.log_error(
			title="merge_automation_field_allowlists: row failed to migrate",
			message=f"{doctype_name}.{fieldname} :: {frappe.get_traceback()}",
		)
		return False
