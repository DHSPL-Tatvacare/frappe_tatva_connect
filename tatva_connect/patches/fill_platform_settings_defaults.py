# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""A number added to Tatva Platform Settings after the Single was first saved shows 0, because frappe applies a field's
default only to a new document. Each Int field with a declared default and no saved value gets that default, read from
meta. Idempotent: a saved value, 0 included, is never touched. No schema_setup twin: a fresh site's Single is born with them."""
import frappe

_SETTINGS = "Tatva Platform Settings"


def execute():
	saved = frappe.db.get_singles_dict(_SETTINGS)
	missing = {df.fieldname: frappe.utils.cint(df.default) for df in frappe.get_meta(_SETTINGS).fields
	           if df.fieldtype == "Int" and df.default and saved.get(df.fieldname) in (None, "")}
	if missing:
		frappe.db.set_single_value(_SETTINGS, missing)
		frappe.clear_cache(doctype=_SETTINGS)
