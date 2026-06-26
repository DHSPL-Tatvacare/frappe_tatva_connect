"""TATVA override of `crm.api.doc.get_data`.

Attaches Frappe's standard `_link_titles` map ({target_doctype}::{value} -> title) for every Link
field whose target doctype declares `show_title_field_in_link`. The list/Kanban/group-by surfaces are
all fed by this one method, so the map rides everywhere — the cell shows the clean title (the
doctype's `title_field`, e.g. `CRM Lead Stage.display_label`) instead of the composite `::` primary
key, while the stored PK is untouched (filter/sort/group still work on it).

ONE generic place, driven by the framework's own `show_title_field_in_link` flag — no per-field,
per-surface, or fork code. Delegates to the unchanged native `get_data` (same pattern as
`access/native_guards`).
"""
import frappe


@frappe.whitelist()
def get_data(**kwargs):
	result = _native_get_data(**kwargs)
	if isinstance(result, dict):
		_attach_link_titles(result)
	return result


def _native_get_data(**kwargs):
	from crm.api.doc import get_data as native

	# This override is dispatched with the full form_dict (cmd, etc.); forward only the args the
	# native signature accepts — frappe.get_newargs is the same filter frappe.call uses.
	return native(**frappe.get_newargs(native, kwargs))


def _attach_link_titles(result):
	rows = result.get("data") or []
	fields = result.get("fields") or []
	if not rows or not fields:
		return

	# Link fields whose target shows its title in links -> (target doctype, its title field).
	link_fields = {}
	for f in fields:
		if f.get("fieldtype") == "Link" and f.get("options") and f.get("fieldname"):
			meta = frappe.get_meta(f["options"])
			if meta.show_title_field_in_link and meta.title_field:
				link_fields[f["fieldname"]] = (f["options"], meta.title_field)
	if not link_fields:
		return

	titles = result.setdefault("_link_titles", {})
	for row in rows:
		for fieldname, (target_dt, title_field) in link_fields.items():
			value = row.get(fieldname)
			if not value:
				continue
			key = f"{target_dt}::{value}"
			if key not in titles:
				# Value read (get_cached_value): the row value is already in the caller's permitted
				# result, so resolving its display label exposes nothing new.
				titles[key] = frappe.get_cached_value(target_dt, value, title_field) or value
