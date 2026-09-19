# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A catalog lead field's VALUE — what holds it, what a surface may do with it, and the ONE reading of it.

Every surface used to decide for itself where a `CRM Lead API Field` row's value lives, and most knew
only a column. Two kinds are not one: a MULTI-VALUE field hangs its selections off the lead
(`lead.multi_value`), and a VIRTUAL field is computed by frappe on read and stored nowhere. A surface that
put either into SQL answered `1054 Unknown column`; one that read the dead column a multi-value field
still carries showed blank in silence.

This module adds only the layer those rules were missing. What a column is stays frappe's answer
(`Meta.get_valid_columns`), where a section's columns live stays `crm_lead_section.sql_source`, which row
is current stays `multirow`, and the selections stay `multi_value`.
"""
import frappe
from frappe.utils import cint, cstr

from tatva_connect.lead import keyvalue, multi_value, multirow
from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section
from tatva_connect.taxonomy import labels

# The section brain's own words for the three kinds a column holds, spoken here so every surface asks one module.
PARENT, CHILD, ANSWER = crm_lead_section.PARENT, crm_lead_section.CHILD, crm_lead_section.ANSWER
# Selections kept as `CRM Lead Multi Value` rows on the lead, never in a column.
MULTI_VALUE = "multi_value"
# Computed by frappe on read (`is_virtual`), never stored.
VIRTUAL = "virtual"

_SQL_KINDS = (PARENT, CHILD, ANSWER)


def is_column(doctype, fieldname):
	"""Is this a real column of the doctype — frappe's own list: standard fields in, virtual and table fields out."""
	return bool(doctype and fieldname) and fieldname in frappe.get_meta(doctype).get_valid_columns()


def kind_of(section, row):
	"""Where a catalog row's value lives, or None where it resolves to nothing a reader can reach."""
	if cint(row.get("is_multi_value")):
		return MULTI_VALUE
	source = crm_lead_section.sql_source(section)
	if source == ANSWER or is_column(section.get("target_doctype"), row.get("fieldname")):
		return source
	df = crm_lead_section.docfield(section.get("target_doctype"), row.get("fieldname"))
	return VIRTUAL if (df and cint(df.get("is_virtual"))) else None


def in_sql(kind):
	"""Can SQL select, filter and sort this kind — is it a column somewhere."""
	return kind in _SQL_KINDS


def on_page(kind):
	"""Can a list page show this kind: any column, plus multi-value selections read for the page."""
	return in_sql(kind) or kind == MULTI_VALUE


def docfield(section, row):
	"""The DocField that types a row's value: the column its selections live in, its section's answer column, or its own."""
	kind = kind_of(section, row)
	if kind == MULTI_VALUE:
		return multi_value.value_field()
	fieldname = section.get("value_field") if kind == ANSWER else row.get("fieldname")
	return crm_lead_section.docfield(section.get("target_doctype"), fieldname)


def keeps_many_rows(section):
	"""Is this a child section that keeps MANY rows? A key-value section's rows are its fields, so it is not."""
	return bool(section.get("is_multi_row") and section.get("child_table_field") and not section.get("is_key_value"))


def _addresses(section, rows):
	"""The row keys a multi-value field's selections may hang at, newest first; one address where a section keeps one row."""
	key = cstr(section.get("row_key_field"))
	if keeps_many_rows(section):
		return [cstr(r.get(key)) for r in multirow.sorted_child_rows(rows, key)]
	first = rows[0] if (rows and section.get("child_table_field")) else None
	return [cstr(first.get(key)) if (first and key) else ""]


def _newest_selections(held, field_key, addresses):
	"""The selections at the newest address holding any, or [] — the same walk `multirow.current_values` takes over a column."""
	for address in addresses:
		values = held.get((cstr(field_key), address))
		if values:
			return values
	return []


def read(doc, section, row):
	"""THE value a field shows on a loaded lead: its current selections, frappe's computed value, or the section's current reading."""
	kind = kind_of(section, row)
	if kind == MULTI_VALUE:
		rows = doc.get(section.get("child_table_field")) if section.get("child_table_field") else []
		return _newest_selections(multi_value.read_all(doc), row.get("field_key"), _addresses(section, rows or []))
	if kind == VIRTUAL:
		# `doc.get` reads only what was stored; frappe computes a virtual field in `get_valid_dict` alone.
		return doc.get_valid_dict().get(row.get("fieldname"))
	if section.get("child_table_field"):
		current = multirow.current_for_section(doc, section)
		return None if current is None else current.get(row.get("fieldname"))
	return doc.get(row.get("fieldname"))


def every_value(doc, section, row, value):
	"""Every value a field is kept under — one per row where its section keeps many, else `value`, the one on show."""
	table = section.get("child_table_field")
	rows = (doc.get(table) or []) if table else []
	many = keeps_many_rows(section)
	if kind_of(section, row) == MULTI_VALUE:
		held = multi_value.read_all(doc)
		field_key = cstr(row.get("field_key"))
		addresses = [cstr(r.get(section.get("row_key_field"))) for r in rows] if many else _addresses(section, rows)
		return [held.get((field_key, address), []) for address in addresses]
	if not many:
		return [value]
	return [r.get(row.get("fieldname")) for r in rows]


def display(df, value):
	"""A Link value's label, a list of labels for selections, or None where the raw value is what a reader sees."""
	if not (df and df.fieldtype == "Link" and df.options and value):
		return None
	if isinstance(value, list):
		return [labels.label(v, df.options) for v in value]
	return labels.title_of(df.options, value)


def as_text(df, value):
	"""The value as one line a person reads: labels over keys, several selections as one answer."""
	shown = display(df, value)
	return keyvalue.answer_of(value if shown is None else shown)


def page_selections(names, parenttype, section, rows):
	"""{(parent, field_key): [value, ...]} for a page of parents — `read`'s multi-value rule, two queries for the whole page."""
	field_keys = [cstr(r.get("field_key")) for r in rows]
	if not (names and field_keys):
		return {}
	held = {}
	for sel in frappe.get_all(  # authz-ok: tier-a — the caller hands parents that already passed its own read gate
		multi_value.DOCTYPE,
		filters={"parent": ["in", names], "parenttype": parenttype, "field_key": ["in", field_keys]},
		fields=["parent", "field_key", "row_key", "value"],
		order_by="idx asc",
		limit_page_length=0,
	):
		held.setdefault(cstr(sel.parent), {}).setdefault((cstr(sel.field_key), cstr(sel.row_key)), []).append(sel.value)
	table = section.get("child_table_field")
	key = cstr(section.get("row_key_field"))
	children = {}
	if table:
		for child in frappe.get_all(  # authz-ok: tier-a — the caller hands parents that already passed its own read gate
			section.get("target_doctype"),
			filters={"parent": ["in", names], "parenttype": parenttype, "parentfield": table},
			fields=["parent", "name", "idx", *([key] if key else [])],
			order_by="idx asc",
			limit_page_length=0,
		):
			children.setdefault(cstr(child.parent), []).append(child)
	return {
		(parent, field_key): _newest_selections(held.get(parent, {}), field_key,
		                                        _addresses(section, children.get(parent, [])))
		for parent in map(cstr, names) for field_key in field_keys
	}
