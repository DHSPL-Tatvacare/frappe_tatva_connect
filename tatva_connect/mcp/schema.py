# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The schema tools — the live doctype structure, and never a row.

Structure comes from `frappe.get_meta`, which is what Desk itself reads to draw a screen. So a label
this server quotes is the label on the person's screen, custom fields and property setters included,
and "always current" is structural rather than maintained. `information_schema` is deliberately not
used: it knows columns and types and nothing an agent needs — no label, no options, no mandatory
flag, no conditional display.

Scope is derived, never typed. The allowlist is three APPS; their modules come from `Module Def` and
their doctypes from `DocType`, so a doctype added to this app tomorrow is in scope tomorrow; a
framework doctype is in scope only once an operator lists it as a readable table (`records.readable`).

The split between the two tools is deliberate. `search_schema` is a FINDER: it reads the DocField and
Custom Field tables directly, because asking `get_meta` about every doctype in scope to answer one
word would be absurd. It can therefore be a little behind a property setter, and it says so by
returning nothing but names. `get_schema` is the AUTHORITY: one doctype, through `get_meta`, checked
against the caller's own read permission before a single field is described.
"""
import functools

import frappe
from frappe.desk.doctype.dashboard_chart.dashboard_chart import get_parent_doctypes
from frappe.desk.form.linked_with import get_linked_doctypes
from frappe.model import no_value_fields
from frappe.permissions import get_role_permissions

from tatva_connect.mcp import ToolError, desk, settings

ALLOWED_APPS = ("tatva_connect", "crm", "wiki")


def _in_scope():
	"""Every doctype of the allowlisted apps, derived from Module Def, plus every table an operator opened to the agents."""
	from tatva_connect.mcp import records

	modules = frappe.get_all("Module Def", filters={"app_name": ["in", ALLOWED_APPS]}, pluck="name")
	described = frappe.get_all("DocType", filters={"module": ["in", modules]}, pluck="name") if modules else []
	return described + [doctype for doctype in records.readable() if doctype not in described]


def can_read(doctype):
	"""May this login read the doctype? A controller that refuses by raising (CRM Control Tower's `only_for`) is a no."""
	try:
		return frappe.has_permission(doctype, "read")
	except frappe.PermissionError:
		return False  # the refusal IS the answer asked for, not an error to surface


def _readable(doctype, scope):
	"""In scope AND the caller may read it. A child table is readable through any parent the caller can read."""
	if doctype not in scope:
		return False
	if frappe.get_meta(doctype).istable:
		return any(can_read(parent) for parent in get_parent_doctypes(doctype) if parent in scope)
	return can_read(doctype)


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
	if field.fieldtype == "Link" and field.options:
		described["url"] = desk.list_url(field.options)
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
	# The get_schema gate, asked once per doctype: a child table's fields count when its parent is readable.
	readable = functools.cache(lambda doctype: _readable(doctype, scope))
	# One row past the cap is fetched so the answer can say there is more.
	doctypes = [name for name in frappe.get_all(
		"DocType", filters=[["name", "in", scope], ["name", "like", pattern]],
		pluck="name", limit=cap + 1) if readable(name)]

	fields = []
	for doctype, filters in (("DocField", {"parenttype": "DocType", "parent": ["in", scope]}),
	                         ("Custom Field", {"dt": ["in", scope]})):
		owner = "parent" if doctype == "DocField" else "dt"
		fields += [{"doctype": row[owner], "field": row.fieldname, "label": row.label, "type": row.fieldtype}
		           for row in frappe.get_all(doctype, filters={**filters, "label": ["like", pattern]},
		                                     fields=[owner, "fieldname", "label", "fieldtype"],
		                                     limit=cap + 1)
		           if readable(row[owner])]

	more = len(doctypes) > cap or len(fields) > cap
	return {"query": query, "doctypes": doctypes[:cap], "fields": fields[:cap], "more": more,
	        "next": ("More matched than shown: use a longer word. " if more else "")
	                + "Call get_schema on a doctype for its full, authoritative field list."}


def get_schema(arguments):
	"""One doctype in full: every field a person meets, and the Desk pages that show them."""
	doctype = (arguments.get("doctype") or "").strip()
	if not doctype:
		raise ToolError("Name a doctype — search_schema finds one from a word.")
	scope = _in_scope()
	if not _readable(doctype, scope):
		raise ToolError(f"'{doctype}' is not something this server describes, or this login cannot read it.")

	meta = frappe.get_meta(doctype)
	return {
		"doctype": meta.name,
		"is_single": bool(meta.issingle),
		"is_child_table": bool(meta.istable),
		"urls": {} if meta.istable else desk.urls_for(meta.name),
		"you_can": [] if meta.istable else _rights(meta),
		"fields": [_describe(field) for field in meta.fields if field.fieldtype not in no_value_fields],
		"relations": _relations(meta, scope),
	}


def _rights(meta):
	"""What this login may do with the doctype, from frappe's own role evaluation — so a step it cannot take is never suggested."""
	granted = get_role_permissions(meta)
	return [ptype for ptype in ("read", "write", "create", "delete") if granted.get(ptype)]


def _relations(meta, scope):
	"""What a field list cannot show: the tables inside this one, and who points at it. Meta and frappe's Connections lookup, never typed."""
	# A child's holders come from get_parent_doctypes: linked_with's `get_parent` branch unpacks a dict as "dt" and raises (frappe 16.52).
	if meta.istable:
		return {"child_tables": [], "linked_from": [{"doctype": parent, "holds_this_as_table": True}
		                                            for parent in get_parent_doctypes(meta.name)
		                                            if parent in scope and can_read(parent)]}
	linked_from = []
	for doctype, info in get_linked_doctypes(meta.name).items():
		if doctype not in scope or not can_read(doctype):
			continue
		entry = {"doctype": doctype}
		if info.get("fieldname"):
			entry["field"] = ", ".join(info["fieldname"])
		if info.get("child_doctype"):
			entry["via_table"] = info["child_doctype"]
		linked_from.append(entry)
	return {
		"child_tables": [{"field": f.fieldname, "label": f.label, "doctype": f.options} for f in meta.get_table_fields()],
		"linked_from": linked_from,
	}
