# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Add the Zydus group, the Liver-Forever program, and the two grains they form.

backfill_crm_grain already ran on every migrated site, and an applied patch is dead — its GRAINS list
now carries these tuples so the off-registry reporter does not flag them, but that edit inserts nothing
here. This is the new line that lands them.

The masters come first: ensure_grains skips any tuple whose axes do not resolve (a Link to a missing
master would throw and fail the migrate), so creating the grain without the group and program would
silently no-op. End state: both masters exist and CRM Grain holds Goodflip::Zydus:: and
Goodflip::Zydus::Liver-Forever. Idempotent; assumes nothing about what ran before.
"""
import frappe

from tatva_connect.patches import backfill_crm_grain

# (doctype, name, the doctype's own name field) — the two masters the Zydus grains Link to.
_MASTERS = (
	("CRM Group", "Zydus", "group_name"),
	("CRM Program", "Liver-Forever", "program_name"),
)


def execute():
	_ensure_masters()
	backfill_crm_grain.ensure_grains()


def _ensure_masters():
	"""Insert each master if absent. The blank-program grain is real: a Zydus lead exists before enrolment."""
	for doctype, name, name_field in _MASTERS:
		if frappe.db.exists(doctype, name):
			continue
		doc = frappe.new_doc(doctype)
		doc.set(name_field, name)
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — migrate/patch, no session user
	frappe.db.commit()
