# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Every edit to one lead, one row per changed value: the lead's Version rows, labelled from the schema, with child rows a save deleted and re-added unchanged left out."""
from collections import Counter

import frappe
from frappe import _
from frappe.utils import cstr

LEAD = "CRM Lead"
SAVES = 500
# Columns every child row carries that describe the row, not what it holds.
ROW_META = {"name", "owner", "creation", "modified", "modified_by", "parent", "parentfield", "parenttype", "idx", "docstatus", "doctype"}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return edit_rows(LEAD, filters.lead, filters.field, _("Lead"))


def edit_rows(doctype, name, field=None, record_label=None):
	"""Every edit to one record of `doctype`, one row per changed value, for any report that shows a record's edit history."""
	frappe.has_permission(doctype, "read", name, throw=True)
	columns = [
		{"label": _("When"), "fieldname": "when", "fieldtype": "Datetime", "width": 160},
		{"label": _("By"), "fieldname": "by", "fieldtype": "Link", "options": "User", "width": 200},
		{"label": _("Section"), "fieldname": "section", "fieldtype": "Data", "width": 190},
		{"label": _("Field"), "fieldname": "field", "fieldtype": "Data", "width": 190},
		{"label": _("Before"), "fieldname": "before", "fieldtype": "Data", "width": 200},
		{"label": _("After"), "fieldname": "after", "fieldtype": "Data", "width": 200},
	]
	versions = frappe.get_all(  # authz-ok: tier-b — frappe.has_permission on the record above is the gate
		"Version", filters={"ref_doctype": doctype, "docname": name},
		fields=["creation", "owner", "data"], order_by="creation desc", limit=SAVES,
	)
	meta = frappe.get_meta(doctype)
	needle = cstr(field).strip().lower()
	rows = []
	for v in versions:
		for section, label, before, after in edits(meta, frappe.parse_json(v.data) or {}, record_label):
			if needle and needle not in label.lower() and needle not in section.lower():
				continue
			rows.append({"when": v.creation, "by": v.owner, "section": section, "field": label, "before": before, "after": after})
	return columns, rows


def edits(meta, data, record_label=None):
	"""The real edits one save made, as (section, field, before, after); `record_label` names the parent's own fields, Lead by default."""
	for fieldname, before, after in data.get("changed") or []:
		yield record_label or _("Lead"), meta.get_label(fieldname), _show(before), _show(after)
	for table, idx, _row, changes in data.get("row_changed") or []:
		child = _child_meta(meta, table)
		for fieldname, before, after in changes:
			yield f"{meta.get_label(table)} · row {idx + 1}", child.get_label(fieldname), _show(before), _show(after)  # frappe counts this one from 0
	added, removed = _unpaired(data.get("added") or [], data.get("removed") or [])
	# A row re-added in the slot it was removed from is that row edited: show only the values that moved.
	slots = {(table, row.get("idx")): row for table, row in removed}
	for table, row in added:
		child = _child_meta(meta, table)
		old = slots.pop((table, row.get("idx")), None)
		if old is None:
			yield meta.get_label(table), _("row added"), "", _summary(child, row)
			continue
		for key in sorted(set(old) | set(row)):
			if _held(key) and (old.get(key) or None) != (row.get(key) or None):
				yield f"{meta.get_label(table)} · row {row.get('idx')}", child.get_label(key), _show(old.get(key)), _show(row.get(key))
	for (table, _idx), row in slots.items():
		yield meta.get_label(table), _("row removed"), _summary(_child_meta(meta, table), row), ""


def _unpaired(added, removed):
	"""Drop each removed row that the same save added back with the same content: a lead save rewrites its child tables."""
	readded = Counter((table, _content(row)) for table, row in added)
	kept_removed = []
	for table, row in removed:
		key = (table, _content(row))
		if readded[key]:
			readded[key] -= 1
		else:
			kept_removed.append((table, row))
	kept_added = []
	for table, row in reversed(added):
		key = (table, _content(row))
		if readded[key]:
			readded[key] -= 1
			kept_added.append((table, row))
	return list(reversed(kept_added)), kept_removed


def _content(row):
	return frappe.as_json({k: v for k, v in sorted((row or {}).items()) if _held(k) and v not in (None, "")})


def _held(key):
	"""A column that holds the row's data; frappe's own `__` flags (`__unsaved` on a re-added row) describe the save."""
	return key not in ROW_META and not key.startswith("__")


def _child_meta(meta, table):
	df = meta.get_field(table)
	return frappe.get_meta(df.options) if df and df.options else meta


def _summary(child, row):
	"""The row's first three filled values, labelled, so an added or removed row reads at a glance."""
	parts = [f"{child.get_label(k)}: {_show(v)}" for k, v in (row or {}).items() if _held(k) and v not in (None, "", 0)]
	return "; ".join(parts[:3])


def _show(value):
	return "" if value is None else cstr(value)[:300]
