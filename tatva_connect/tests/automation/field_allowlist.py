# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Shared seed helpers for the merged `CRM Automation Field` allowlist — ONE place for the allowlist's
shape across every automation test (mirrors the engine's own single-brain automation.fields). Kills the
per-file _seed_allowlist / _seed_watchable copies the pre-merge tests each carried."""
import frappe

DOCTYPE = "CRM Automation Field"


def seed_watchable(doctype, fieldname):
	"""An enabled can_watch row (blank grain — watch is grain-independent)."""
	return _ensure({"doctype_name": doctype, "fieldname": fieldname, "can_watch": 1, "enabled": 1})


def seed_settable(doctype, fieldname, vertical="", group="", program="", child_table_field="", is_row_key=0):
	"""An enabled can_set row at a grain (blank axis = wildcard)."""
	return _ensure({
		"doctype_name": doctype, "fieldname": fieldname, "can_set": 1, "enabled": 1,
		"vertical": vertical, "group": group, "program": program,
		"child_table_field": child_table_field, "is_row_key": is_row_key,
	})


def _ensure(payload):
	"""Insert the row, or OR the capability flags onto an existing one (idempotent across re-runs)."""
	key = {
		"doctype_name": payload["doctype_name"], "fieldname": payload["fieldname"],
		"child_table_field": payload.get("child_table_field", "") or "",
		"vertical": payload.get("vertical", "") or "", "group": payload.get("group", "") or "",
		"program": payload.get("program", "") or "",
	}
	name = frappe.db.get_value(DOCTYPE, key)
	if name:
		doc = frappe.get_doc(DOCTYPE, name)
		for flag in ("can_watch", "can_set", "is_row_key"):
			if payload.get(flag):
				setattr(doc, flag, 1)
		doc.enabled = 1
		doc.save(ignore_permissions=True)
		return name
	return frappe.get_doc({"doctype": DOCTYPE, **payload}).insert(ignore_permissions=True).name


def clear(doctype=None):
	"""Delete all merged-allowlist rows (optionally scoped to a doctype) — test teardown."""
	frappe.db.delete(DOCTYPE, {"doctype_name": doctype} if doctype else {})
