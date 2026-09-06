# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The schema tools — the live doctype structure, and never a row.

Structure comes from `frappe.get_meta`, which is what Desk itself reads to draw a screen. So a label
this server quotes is the label on the person's screen, custom fields and property setters included,
and "always current" is structural rather than maintained. `information_schema` is deliberately not
used: it knows columns and types and nothing an agent needs — no label, no options, no mandatory
flag, no conditional display.

Scope is derived, never typed. The allowlist is three APPS; their modules come from `Module Def` and
their doctypes from `DocType`, so a doctype added to this app tomorrow is in scope tomorrow and a
doctype belonging to the framework never is.

The split between the two tools is deliberate. `search_schema` is a FINDER: it reads the DocField and
Custom Field tables directly, because asking `get_meta` about every doctype in scope to answer one
word would be absurd. It can therefore be a little behind a property setter, and it says so by
returning nothing but names. `get_schema` is the AUTHORITY: one doctype, through `get_meta`, checked
against the caller's own read permission before a single field is described.
"""
import frappe
from frappe.model import no_value_fields

from tatva_connect.mcp import ToolError, desk, settings

ALLOWED_APPS = ("tatva_connect", "crm", "wiki")


def _in_scope():
	"""Every doctype belonging to the allowlisted apps. Derived from Module Def, never hand-typed."""
	modules = frappe.get_all("Module Def", filters={"app_name": ["in", ALLOWED_APPS]}, pluck="name")
	return frappe.get_all("DocType", filters={"module": ["in", modules]}, pluck="name") if modules else []


def _readable(doctype, scope):
	"""In scope AND the caller may read it. Both questions, one answer, asked in one place."""
	return doctype in scope and frappe.has_permission(doctype, "read")


def _describe(field):
	"""One field, in the words the person will see. Empty properties are left out, not sent as null."""
	described = {
		"field": field.fieldname,
		"label": field.label or field.fieldname,
		"type": field.fieldtype,
	}
	for key, value in (("options", field.options), ("shown_when", field.depends_on)):
		if value:
			described[key] = value
	for key, flag in (("mandatory", field.reqd), ("read_only", field.read_only), ("hidden", field.hidden)):
		if flag:
			described[key] = True
	return described


# -- tools --------------------------------------------------------------------
def search_schema(arguments):
	"""Find the doctype or field behind a business word — 'campaign', 'dropped reason'."""
	query = (arguments.get("query") or "").strip()
	if not query:
		raise ToolError("Give a word to look for — a field label, or part of a doctype name.")

	scope, pattern = _in_scope(), f"%{query}%"
	cap = settings.config()["schema_max_hits"]
	doctypes = [name for name in frappe.get_all(
		"DocType", filters=[["name", "in", scope], ["name", "like", pattern]],
		pluck="name", limit=cap) if frappe.has_permission(name, "read")]

	fields = []
	for doctype, filters in (("DocField", {"parenttype": "DocType", "parent": ["in", scope]}),
	                         ("Custom Field", {"dt": ["in", scope]})):
		owner = "parent" if doctype == "DocField" else "dt"
		fields += [{"doctype": row[owner], "field": row.fieldname, "label": row.label, "type": row.fieldtype}
		           for row in frappe.get_all(doctype, filters={**filters, "label": ["like", pattern]},
		                                     fields=[owner, "fieldname", "label", "fieldtype"],
		                                     limit=cap)
		           if frappe.has_permission(row[owner], "read")]

	return {"query": query, "doctypes": doctypes, "fields": fields[:cap],
	        "next": "Call get_schema on a doctype for its full, authoritative field list."}


def get_schema(arguments):
	"""One doctype in full: every field a person meets, and the Desk pages that show them."""
	doctype = (arguments.get("doctype") or "").strip()
	if not doctype:
		raise ToolError("Name a doctype — search_schema finds one from a word.")
	if not _readable(doctype, _in_scope()):
		raise ToolError(f"'{doctype}' is not something this server describes, or this login cannot read it.")

	meta = frappe.get_meta(doctype)
	return {
		"doctype": meta.name,
		"is_single": bool(meta.issingle),
		"urls": desk.urls_for(meta.name),
		"fields": [_describe(field) for field in meta.fields if field.fieldtype not in no_value_fields],
	}
