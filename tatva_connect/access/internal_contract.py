"""Seed the per-grain INTERNAL visibility contracts — the same tick mechanism the partner API uses.

Phase 5A (safety-first): this ADDS `is_internal=1` rows to `CRM Lead API Mapping` whose ticked
`allowed_fields` ARE the fields an internal user in that grain may see. The tick set is defined by
the EXISTING grain_* logic (`entitlement.field_in_grains`) so switching a reader from grain_* to the
contract changes nothing. grain_* stays live and still read — nothing is removed here.

A grain is a `(vertical, group, program)` tuple. The world of grains = the DISTINCT non-blank grains a
field can carry (`CRM Lead API Field` grain_* tags) UNION the grains of existing mappings
(vertical/crm_group/program) UNION the grains of live CRM Lead Assignment Rules — a union so every grain
an internal user could be entitled to (via `entitled_grains`) has a contract the reader can resolve.
"""
import frappe

from tatva_connect.access import entitlement

# Fixed contract_name for every internal row; the grain axes make the autoname id unique per grain.
_CONTRACT_NAME = "Internal Visibility"


def _grain_tuple(vertical, group, program):
	return (vertical or "", group or "", program or "")


def _contract_grains():
	"""Union of the non-blank grains on catalog fields, existing mappings, and live CRM Lead Assignment Rules."""
	grains = set()
	for r in frappe.get_all(
		"CRM Lead API Field", fields=["grain_vertical", "grain_group", "grain_program"]
	):
		g = _grain_tuple(r.grain_vertical, r.grain_group, r.grain_program)
		if any(g):
			grains.add(g)
	for m in frappe.get_all(
		"CRM Lead API Mapping", fields=["vertical", "crm_group", "program"]
	):
		g = _grain_tuple(m.vertical, m.crm_group, m.program)
		if any(g):
			grains.add(g)
	# Assignment Rule grains an internal user can be entitled to (disabled rules grant no access).
	for a in frappe.get_all(
		"Assignment Rule", filters={"document_type": "CRM Lead", "disabled": 0},
		fields=["grain_vertical", "grain_group", "grain_program"],
	):
		g = _grain_tuple(a.grain_vertical, a.grain_group, a.grain_program)
		if any(g):
			grains.add(g)
	return grains


def _ticked_keys(grain):
	"""The field_keys visible in `grain` per the EXISTING grain_* brain — the tick set, by definition."""
	keys = []
	for row in frappe.get_all(
		"CRM Lead API Field",
		fields=["field_key", "grain_vertical", "grain_group", "grain_program"],
		order_by="field_key asc",
	):
		if entitlement.field_in_grains(row, {grain}):
			keys.append(row.field_key)
	return keys


def _existing_internal():
	"""{grain_tuple: contract name} for the is_internal=1 rows already present (idempotent upsert key)."""
	out = {}
	for m in frappe.get_all(
		"CRM Lead API Mapping", filters={"is_internal": 1},
		fields=["name", "vertical", "crm_group", "program"],
	):
		out[_grain_tuple(m.vertical, m.crm_group, m.program)] = m.name
	return out


def ensure_internal_contracts():
	"""Idempotent: one is_internal=1 CRM Lead API Mapping per grain, its Allowed Fields = the grain's
	visible field_keys (per grain_*). Re-running produces the same rows and the same ticks."""
	existing = _existing_internal()
	for grain in sorted(_contract_grains()):
		vertical, group, program = grain
		keys = _ticked_keys(grain)
		if grain in existing:
			doc = frappe.get_doc("CRM Lead API Mapping", existing[grain])
		else:
			doc = frappe.new_doc("CRM Lead API Mapping")
			doc.contract_name = _CONTRACT_NAME
			doc.partner_user = None
			doc.vertical = vertical
			doc.crm_group = group or None
			doc.program = program or None
		doc.is_internal = 1
		doc.enabled = 1
		doc.set("allowed_fields", [])
		for key in keys:
			doc.append("allowed_fields", {"field": key})
		doc.save(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
	frappe.db.commit()
