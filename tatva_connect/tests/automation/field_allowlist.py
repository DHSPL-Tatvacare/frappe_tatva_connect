# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Shared seed helpers for the automation allowlist — now the resource catalogs + the ONE grain brain.

Capabilities are Check flags on `CRM Lead API Field` / `CRM Task Type Field`; a field is settable IN a
grain iff that grain's internal contract (`CRM Lead API Mapping` is_internal=1) ticks its field_key — the
same brain `access.entitlement.field_in_grains_via_contract` reads. So `seed_settable` ticks the CONTRACT,
never a per-row grain flag. Class-level seeds commit (FrappeTestCase rolls back per test, not per class),
so every seed is recorded and `clear()` undoes exactly it.
"""
import frappe

_LEAD = "CRM Lead"
_TASK = "CRM Task"
_LEAD_CATALOG = "CRM Lead API Field"
_TASK_CATALOG = "CRM Task Type Field"
_MAPPING = "CRM Lead API Mapping"
_TICKS_CACHE = "tatva_connect:internal_contract_ticks"  # request_cache bucket on frappe.local

# What this run's seeds created, so clear() reverses precisely that (commit-visible class-level state).
_seeded_flags = []  # (catalog, name)
_seeded_rows = []   # (doctype, name) — a row a seed materialised (delete on clear)
_seeded_ticks = []  # (mapping_name, field_key) — a contract tick a seed appended


def _catalog_for(doctype):
	return {_LEAD: _LEAD_CATALOG, _TASK: _TASK_CATALOG}.get(doctype)


def _bust_ticks_cache():
	setattr(frappe.local, _TICKS_CACHE, None)


def _flag_rows(doctype, fieldname, flag):
	catalog = _catalog_for(doctype)
	names = frappe.get_all(catalog, filters={"fieldname": fieldname}, pluck="name") if catalog else []
	for name in names:
		frappe.db.set_value(catalog, name, flag, 1)
		_seeded_flags.append((catalog, name))
	return names


def seed_readable(doctype, fieldname):
	"""Tick can_read — testable by a criterion; no change to it fires a rule."""
	_flag_rows(doctype, fieldname, "can_read")
	return fieldname


def seed_watchable(doctype, fieldname):
	"""Tick can_watch — a change fires a rule (implies readable in fields.py)."""
	_flag_rows(doctype, fieldname, "can_watch")
	return fieldname


def seed_settable(doctype, fieldname, vertical="", group="", program="", child_table_field="", is_row_key=0):
	"""Tick can_set AND tick the field_key in grain (vertical, group, program)'s internal contract. A lead
	field absent from the catalog is materialised as a `lead:*` parent row (faithful port of the old
	allowlist-row seed — a real settable lead field, rolled back with the test)."""
	if doctype == _LEAD:
		names = frappe.get_all(_LEAD_CATALOG, filters={"fieldname": fieldname}, pluck="name")
		if not names:
			doc = frappe.get_doc({
				"doctype": _LEAD_CATALOG, "field_key": f"lead:{fieldname}", "label": fieldname,
				"section": "lead", "fieldname": fieldname,
			}).insert()
			_seeded_rows.append((_LEAD_CATALOG, doc.name))
			names = [doc.name]
		for name in names:  # name == field_key (autoname field:field_key)
			frappe.db.set_value(_LEAD_CATALOG, name, "can_set", 1)
			_seeded_flags.append((_LEAD_CATALOG, name))
			_tick_contract(name, vertical, group, program)
		_bust_ticks_cache()
		return names[0]
	_flag_rows(doctype, fieldname, "can_set")
	return fieldname


def _tick_contract(field_key, vertical, group, program):
	"""Ensure grain (vertical, group, program)'s is_internal=1 CRM Lead API Mapping ticks field_key."""
	name = frappe.db.get_value(_MAPPING, {
		"is_internal": 1, "vertical": vertical or "", "crm_group": group or "", "program": program or "",
	})
	if name:
		doc = frappe.get_doc(_MAPPING, name)
		if any(f.field == field_key for f in doc.allowed_fields):
			return
		doc.append("allowed_fields", {"field": field_key})
		doc.save()
		_seeded_ticks.append((name, field_key))
		return
	doc = frappe.get_doc({
		"doctype": _MAPPING, "contract_name": "Internal Visibility", "partner_user": None,
		"vertical": vertical or None, "crm_group": group or None, "program": program or None,
		"is_internal": 1, "enabled": 1, "allowed_fields": [{"field": field_key}],
	}).insert()
	_seeded_rows.append((_MAPPING, doc.name))


def clear(doctype=None):
	"""Undo exactly what the seeds created this run — contract ticks, then flags, then materialised rows."""
	for mapping_name, field_key in _seeded_ticks:
		if frappe.db.exists(_MAPPING, mapping_name):
			doc = frappe.get_doc(_MAPPING, mapping_name)
			doc.set("allowed_fields", [f for f in doc.allowed_fields if f.field != field_key])
			doc.save()
	_seeded_ticks.clear()
	for catalog, name in _seeded_flags:
		if frappe.db.exists(catalog, name):
			frappe.db.set_value(catalog, name, {"can_read": 0, "can_watch": 0, "can_set": 0})
	_seeded_flags.clear()
	for dt, name in _seeded_rows:
		if frappe.db.exists(dt, name):
			frappe.delete_doc(dt, name, force=1)
	_seeded_rows.clear()
	_bust_ticks_cache()
	frappe.db.commit()
