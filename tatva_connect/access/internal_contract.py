"""Seed the per-grain INTERNAL visibility contracts — the same tick mechanism the partner API uses.

Each grain gets one `is_internal=1` `CRM Lead API Mapping` whose ticked `allowed_fields` ARE the fields an
internal user in that grain may see. The tick set is the PRIMARY seed `internal_contract_seed.GRAIN_FIELDS`
(the frozen Phase-9 snapshot) — NOT the retired grain_* columns, which no longer exist on the catalog.

A grain is a `(vertical, group, program)` tuple. The world of grains = the GRAIN_FIELDS keys UNION the
grains of existing internal mappings UNION the grains of live CRM Lead Assignment Rules — a union so every
grain an internal user could be entitled to (via `entitled_grains`) has a contract the reader can resolve.
"""
import frappe

from tatva_connect.access.internal_contract_seed import GRAIN_FIELDS

# Fixed contract_name for every internal row; the grain axes make the autoname id unique per grain.
_CONTRACT_NAME = "Internal Visibility"


def _grain_tuple(vertical, group, program):
	return (vertical or "", group or "", program or "")


def _contract_grains():
	"""Union of the GRAIN_FIELDS keys, existing internal mappings, and live CRM Lead Assignment Rules."""
	grains = set(GRAIN_FIELDS.keys())
	for m in frappe.get_all(
		"CRM Lead API Mapping", filters={"is_internal": 1},
		fields=["vertical", "crm_group", "program"],
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


def _row_key_keys(section_keys, catalog):
	"""A multi-row section's ROW KEY travels with its section: granting `lab` values without `report_date`
	leaves a row nobody can date — the Data tab shows values with no report date, `latest_by` has no visible
	key, and an intake form cannot map the date it collects. The key is structural (declared once on
	CRM Lead Section), not a per-grain choice, so the seeder adds it wherever the section is granted."""
	out = set()
	for section in section_keys:
		row_key = frappe.db.get_value("CRM Lead Section", section, "row_key_field")
		if not row_key:
			continue
		key = frappe.db.get_value(
			"CRM Lead API Field", {"section": section, "fieldname": row_key}, "field_key"
		)
		if key and key in catalog:
			out.add(key)
	return out


def _ticked_keys(grain, catalog):
	"""The field_keys visible in `grain` per the PRIMARY seed — the tick set, by definition, intersected with
	the catalog rows that actually exist (`catalog`), PLUS the row key of every section the grain can see.
	The old seeder only ever iterated existing catalog rows, so this stays byte-identical where the full
	catalog is present, and never ticks a Link to an absent row on a partial catalog (fresh install seeds the
	catalog before this runs; a missing key simply waits its turn)."""
	keys = {k for k in GRAIN_FIELDS.get(grain, []) if k in catalog}
	# Section comes off the catalog row (a Link), never from splitting field_key on ':' — that shape is a naming convention, not the routing brain.
	sections = set(frappe.get_all(
		"CRM Lead API Field", filters={"field_key": ("in", list(keys))}, pluck="section", distinct=True
	)) if keys else set()
	return sorted(keys | _row_key_keys(sections, catalog))


def fields_for_grain(grain, catalog=None):
	"""The field_keys a grain exposes — the ONE brain both internal contracts and partner mappings tick from.
	`grain` is a (vertical, group, program) tuple; pass a prebuilt `catalog` set to skip re-querying."""
	if catalog is None:
		catalog = set(frappe.get_all("CRM Lead API Field", pluck="field_key"))
	return _ticked_keys(grain, catalog)


_AXIS_DOCTYPE = (("CRM Vertical", 0), ("CRM Group", 1), ("CRM Program", 2))


def _masters_exist(grain):
	"""Every NON-BLANK axis of `grain` resolves to a real master (CRM Vertical/Group/Program). GRAIN_FIELDS is
	a hardcoded snapshot, so on a bare fresh site (masters not yet seeded) a grain can name a vertical that does
	not exist — creating its contract would throw LinkValidationError and fail the INSTALL. The old grain_*
	derivation could never do this (its axes were Link columns, always valid), so this restores that safety:
	seed a grain only once its masters exist; a later migrate (post master-data seed) picks it up. No-op on a
	populated site — every axis already resolves, so nothing is skipped there."""
	for doctype, i in _AXIS_DOCTYPE:
		if grain[i] and not frappe.db.exists(doctype, grain[i]):
			return False
	return True


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
	visible field_keys (per GRAIN_FIELDS). Re-running produces the same rows and the same ticks."""
	existing = _existing_internal()
	catalog = set(frappe.get_all("CRM Lead API Field", pluck="field_key"))
	for grain in sorted(_contract_grains()):
		if grain not in existing and not _masters_exist(grain):
			continue  # fresh site: master data not seeded yet — a later migrate seeds this grain's contract
		vertical, group, program = grain
		keys = fields_for_grain(grain, catalog)
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
