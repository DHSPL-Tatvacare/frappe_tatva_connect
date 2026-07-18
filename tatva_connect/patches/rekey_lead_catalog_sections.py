# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Point every lead catalog row at its section, and name `mx` for what it is.

Three end states. The seven `CRM Lead Section` rows exist — seeded through the SAME function
after_migrate calls, so the patch and the hook can never declare two structures. Every lead field_key
carries a `section` Link, and `mx` (LeadSquared's `mx_` custom-field prefix — it named the source,
never the fact) becomes `metrics`, re-keyed with `rename_doc` so the Links on CRM Lead API Mapping
Field and CRM Lead Field Restriction follow the key instead of orphaning; a Data field holding a key
is exactly what left 397 rows unreachable when the task types were re-keyed. `is_row_key` goes: the
section carries the row key now, and _build_catalog was the column's only reader.

The 397 CRM Task rows in this table are not lead fields and have no lead section, so they keep a blank
`section` until the phase that removes them. Idempotent; assumes nothing about what ran before.
"""
import frappe

from tatva_connect.partner_api import section_seed
from tatva_connect.patches import _schema

DT = "CRM Lead API Field"
TABLE = "tabCRM Lead API Field"


def execute():
	section_seed.ensure_rows()
	_rekey_mx()
	_link_sections()
	_drop_is_row_key()
	frappe.db.commit()


def _rekey_mx():
	"""rename_doc carries every Link with the key, and rewrites field_key itself (autoname `field:`)."""
	for name in frappe.get_all(DT, filters={"field_key": ["like", "mx:%"]}, pluck="name"):
		new = "metrics:" + name.split(":", 1)[1]
		if frappe.db.exists(DT, new):
			continue
		frappe.rename_doc(DT, name, new)


def _link_sections():
	"""The Link is the fact; section_key mirrored it until its last reader moved. Once the mirror column
	is dropped (or on a fresh install where it never existed), rows are seeded with `section` directly and
	there is nothing to mirror — so guard on the column and no-op."""
	if not frappe.db.has_column(DT, "section_key"):
		return
	sections = set(frappe.get_all("CRM Lead Section", pluck="name"))
	for r in frappe.get_all(DT, fields=["name", "section", "section_key"]):
		want = "metrics" if r.section_key == "mx" else r.section_key
		if want not in sections:
			continue  # a CRM Task row: not a lead field, so it has no lead section
		if r.section == want and r.section_key == want:
			continue
		frappe.db.set_value(DT, r.name, {"section": want, "section_key": want}, update_modified=False)


def _drop_is_row_key():
	"""Set on 1 row of 607 — which is why Lab was called single-row. The section states it once now."""
	_schema.refresh(TABLE)
	if not frappe.db.has_column(DT, "is_row_key"):
		return
	_schema.ddl(f"ALTER TABLE `{TABLE}` DROP COLUMN `is_row_key`", TABLE)
