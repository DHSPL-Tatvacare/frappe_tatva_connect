# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""CRM Cohort Pace, Contact Cap and MCP Settings fold into Tatva Platform Settings → Rate Limits: each saved value moves to its
prefixed field, the old Single's rows leave tabSingles (deleting a DocType never clears them), the DocType goes, and Tatva
Setup drops its three links. Idempotent: a site without the old DocType skips it. No schema_setup twin."""
import frappe

from tatva_connect.patches import _desk, _schema

_SETTINGS = "Tatva Platform Settings"
_MCP = ("window_seconds", "per_user_rate", "global_rate", "search_max_hits", "schema_max_hits", "page_characters")
_MOVES = {
	"CRM Cohort Pace Settings": {"chunk_size": "pace_chunk_size", "interval_seconds": "pace_interval_seconds"},
	"CRM Contact Cap Settings": {f: f"contact_cap_{f}" for f in ("enabled", "max_contacts", "window_count", "window_unit")},
	"CRM MCP Settings": {f: f"mcp_{f}" for f in _MCP},
}


def execute():
	for old, fields in _MOVES.items():
		if not frappe.db.exists("DocType", old):
			continue
		saved = frappe.db.get_singles_dict(old)
		values = {new: saved[f] for f, new in fields.items() if saved.get(f) not in (None, "")}
		if values:
			frappe.db.set_single_value(_SETTINGS, values)
		frappe.db.delete("Singles", {"doctype": old})
		_schema.drop_doctype(old)  # the DocType, its Custom DocPerm rows and any table, as every retired doctype goes
	frappe.clear_cache(doctype=_SETTINGS)
	_desk.reimport_all([
		("tatva_setup", "workspace", "tatva_setup", "tatva_setup.json"),
		("tatva_setup", "sidebar", "tatva_setup", "tatva_setup.json"),
	])
