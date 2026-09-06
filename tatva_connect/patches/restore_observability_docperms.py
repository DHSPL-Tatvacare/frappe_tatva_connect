# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The two observability log doctypes go back to being governed by their own JSON.

THE DEFECT, MEASURED ON UAT. Opening the Observability desk answered `You don't have permission to get
a report on: CRM API Request Log` for a System Manager — a role the doctype's JSON grants `report` to.
The reason is in `permissions.py:get_valid_perms`: a standard DocPerm is used only when the doctype has
NO `Custom DocPerm` rows. Two existed, both `report = 0`, so the shipped declaration was ignored whole
and every chart on that page — five on the request log, ten on the metric — refused.

Rows like those come from the Role Permissions Manager. Nothing in code can create or delete them, and
a doctype re-import does not touch them, so they outlive every deploy: while they exist, the JSON is
decoration and any future permission change made in code silently does nothing on that site.

WHAT THIS DOES. Per doctype, it removes the override so the JSON governs again — and only ever when
removing it takes nothing away. A Custom row that grants something the JSON does not is somebody's
deliberate widening: that doctype is reported and left exactly as it is, the same rule every access
seed follows. The flags compared are read off `Custom DocPerm`'s own meta, never a typed list.

Paired with the JSONs in the same commit, which add `Automation Manager` — the role that can open the
Observability workspace and, until now, had no grant of its own on either doctype.

Idempotent: a second run finds no rows and does nothing.
"""
import frappe

DOCTYPES = ("CRM API Request Log", "CRM API Metric")


def _flags():
	"""Every permission a DocPerm can carry, as the doctype itself declares them."""
	return [f.fieldname for f in frappe.get_meta("Custom DocPerm").fields if f.fieldtype == "Check"]


def _declared(doctype, flags):
	"""What the shipped JSON grants, by role, as model sync wrote it into DocPerm."""
	rows = frappe.get_all("DocPerm", filters={"parent": doctype}, fields=["role", *flags])
	return {row.role: row for row in rows}


def execute():
	flags = _flags()
	for doctype in DOCTYPES:
		overrides = frappe.get_all("Custom DocPerm", filters={"parent": doctype},
		                           fields=["name", "role", *flags])
		if not overrides:
			continue

		declared = _declared(doctype, flags)
		widened = [
			f"{row.role}.{flag}" for row in overrides for flag in flags
			if row.get(flag) and not (declared.get(row.role) or {}).get(flag)
		]
		if widened:
			print(f"{doctype}: left alone — the override grants {widened}, which the JSON does not")
			continue

		for row in overrides:
			frappe.delete_doc("Custom DocPerm", row.name, force=True, ignore_permissions=True)  # authz-ok: tier-a — patch, runs as Administrator
		print(f"{doctype}: dropped {len(overrides)} override(s) — {[r.role for r in overrides]}; the JSON governs again")

	frappe.db.commit()
	frappe.clear_cache()
