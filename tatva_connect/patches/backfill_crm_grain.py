"""Populate the CRM Grain registry with the curated set of valid (vertical, group, program) combinations.

END STATE: every tuple in `GRAINS` exists as a CRM Grain row. Nothing else is asserted — rows an operator
added by hand are left alone, and re-running inserts only what is missing (the composite name is the
idempotency key, so a second run finds every row already there).

The set below was derived from the live bench on 2026-07-22 as (distinct grain tuples on real CRM Leads)
UNION (the is_internal contract grains), then CURATED. It is deliberately not the raw union: the registry
is authoritative config, and bulk-inserting whatever the lead table holds would enshrine its typos as
valid business slices. One tuple was rejected on exactly that ground — see REJECTED below.

Blank program is a real combination, not a gap: a lead exists before programme enrolment (9 live Anaya
leads sit there) and a group may be declared as a whole region (Goodflip/Insurers).

A fresh site gets these from the seed (db-seeds/2026-07-22-crm-grain.sql); this heals an existing one.
"""
import frappe

from tatva_connect.taxonomy import grain as taxonomy_grain

# The curated registry. (vertical, group, program) — "" program means the combination has no programme axis.
GRAINS = [
	("GoodFlip", "India", "Inside-Sales"),
	("GoodFlip", "Insurers", ""),
	("GoodFlip", "Insurers", "Niva-Bupa"),
	("Goodflip-Care", "Anaya", ""),
	("Goodflip-Care", "Anaya", "Nivolumab"),
	("Goodflip-Care", "Anaya", "Sigrima"),
	("Goodflip-Care", "Anaya", "Tukavo"),
	("Goodflip-Care", "Anaya", "Ujvira"),
	("Goodflip-Care", "Zydus", "Liver-Forever"),
	("TatvaPractice", "India", "Field-Sales"),
	("TatvaPractice", "India", "Inside-Sales"),
]

# REJECTED, and why — kept here so a future backfill does not silently re-adopt it from the lead table.
# ("Goodflip", "Goodflip", "Inside-Sales") — 1 lead. Its group is the VERTICAL's name; the real Goodflip
# groups are India and Insurers. A data error, not a business slice. It surfaces in the off-registry
# report below as a cleanup item rather than being blessed into config.

_AXIS_DOCTYPE = (("CRM Vertical", 0), ("CRM Group", 1), ("CRM Program", 2))


def grain_name(vertical, group, program) -> str:
	"""The composite key — must match the doctype's `format:{vertical}::{group}::{program}` autoname."""
	return f"{vertical or ''}::{group or ''}::{program or ''}"


def _masters_exist(grain) -> bool:
	"""Every non-blank axis resolves to a real master. GRAINS is a static snapshot, so on a bare fresh site
	(masters not yet seeded) a tuple can name a vertical that does not exist yet — inserting it would throw
	LinkValidationError and fail the INSTALL. Skip it; the seed or a later migrate picks it up. No-op on a
	populated site. Same guard, same reason as access/internal_contract._masters_exist."""
	for doctype, i in _AXIS_DOCTYPE:
		if grain[i] and not frappe.db.exists(doctype, grain[i]):
			return False
	return True


def ensure_grains() -> list:
	"""Insert the curated tuples that are missing. Returns the names inserted (empty on a replay).

	Each tuple is spelled the way its MASTER spells it, not the way this list happens to. The Link
	validates case-insensitively, so a literal of 'Goodflip' against a master of 'GoodFlip' inserts
	happily — and mints a composite key in a spelling nothing else in the app uses. That is how this
	table came to hold `Goodflip::India::Inside-Sales` beside `GoodFlip::Insurers::`."""
	inserted = []
	for raw in GRAINS:
		grain = tuple(taxonomy_grain.canon(axis, value) for axis, value in zip(taxonomy_grain.AXES, raw))
		name = grain_name(*grain)
		if frappe.db.exists("CRM Grain", name):
			continue
		if not _masters_exist(grain):
			continue  # fresh site: master data not seeded yet
		doc = frappe.new_doc("CRM Grain")
		doc.vertical, doc.group, doc.program = grain[0], grain[1], grain[2] or None
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — migrate/patch, no session user
		inserted.append(name)
	frappe.db.commit()
	return inserted


def off_registry_lead_grains() -> list:
	"""Lead grains that are NOT registry rows — the cleanup list, so bad data surfaces instead of being
	enshrined. Returns [{vertical, group, program, leads}], most-leads first. Reporting only: it changes
	nothing and blocks nothing."""
	registry = set(frappe.get_all("CRM Grain", pluck="name"))
	rows = frappe.db.sql(
		"""
		SELECT COALESCE(custom_vertical, '') AS vertical,
		       COALESCE(custom_group, '') AS `group`,
		       COALESCE(custom_current_program, '') AS program,
		       COUNT(*) AS leads
		FROM `tabCRM Lead`
		GROUP BY 1, 2, 3
		""",
		as_dict=True,
	)
	off = [
		r for r in rows
		if any((r.vertical, r.group, r.program)) and grain_name(r.vertical, r.group, r.program) not in registry
	]
	return sorted(off, key=lambda r: -r.leads)


def execute():
	inserted = ensure_grains()
	total = frappe.db.count("CRM Grain")
	print(f"CRM Grain: {len(inserted)} inserted, {total} rows total.")
	for r in off_registry_lead_grains():
		print(
			f"CRM Grain: OFF-REGISTRY (cleanup, not blocking) — "
			f"({r.vertical!r}, {r.group!r}, {r.program!r}) on {r.leads} lead(s)."
		)
