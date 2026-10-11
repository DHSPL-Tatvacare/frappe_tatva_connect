# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The bulk lane: a page of records read as sets, one query per table, never one record at a time."""
from collections import defaultdict

import frappe
from frappe.model.utils import is_virtual_doctype
from frappe.utils import cstr


def docs(doctype, names, filters=None):
	"""{name: document} for `names` matching `filters`, each built as `frappe.get_doc` loads one: every column, every child table by idx."""
	names = sorted({cstr(n) for n in names if n})
	if not names or None in (filters or {}):
		return {}  # a filter naming no column is the single lane's to refuse, record by record
	rows = frappe.db.get_values(doctype, {"name": ["in", names], **(filters or {})}, "*", as_dict=True)
	if not rows:
		return {}
	children = defaultdict(lambda: defaultdict(list))
	for df in frappe.get_meta(doctype).get_table_fields():
		if is_virtual_doctype(df.options):
			continue  # get_doc never queries a virtual child table either
		for row in frappe.db.get_values(df.options, {"parent": ["in", [cstr(r.name) for r in rows]], "parenttype": doctype,
		                                             "parentfield": df.fieldname}, "*", as_dict=True, order_by="idx asc"):
			children[row.parent][df.fieldname].append(row)
	return {cstr(r.name): frappe.get_doc({**r, **children[cstr(r.name)], "doctype": doctype}) for r in rows}


def names_within(doctype, names, fence):
	"""The `names` of `doctype` inside `fence`, in one query; empty when the fence names no column, so every record is refused one by one."""
	names = sorted({cstr(n) for n in names if n})
	if not names or None in fence:
		return set()
	return {cstr(n) for n in frappe.db.get_values(doctype, {"name": ["in", names], **fence}, "name", pluck=True)}


def on_line(held, mp, lead_of):
	"""The held records whose lead is on the caller's line: `scoped_by_lead` asked of a whole page in one query."""
	from tatva_connect.api._base import grain_fence

	leads = {name: lead_of(record) for name, record in held.items()}
	within = names_within("CRM Lead", leads.values(), grain_fence(mp, "CRM Lead"))
	return {name: held[name] for name, lead in leads.items() if lead and cstr(lead) in within}
