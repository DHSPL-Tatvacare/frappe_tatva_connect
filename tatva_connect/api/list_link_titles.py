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
		_attach_link_titles(result, kwargs.get("doctype"))
	return result


def _native_get_data(**kwargs):
	from crm.api.doc import get_data as native

	# This override is dispatched with the full form_dict (cmd, etc.); forward only the args the
	# native signature accepts — frappe.get_newargs is the same filter frappe.call uses.
	return native(**frappe.get_newargs(native, kwargs))


def _attach_link_titles(result, doctype=None):
	rows = result.get("data") or []
	if not rows:
		return

	# Static Link fields (target doctype is fixed) from the result's field list.
	link_fields = {}  # fieldname -> fixed target doctype
	for f in result.get("fields") or []:
		if f.get("fieldtype") == "Link" and f.get("options") and f.get("fieldname"):
			link_fields[f["fieldname"]] = f["options"]

	# Dynamic Link fields (target doctype is per-row, the value of the `options` field) from the
	# listed doctype's meta — e.g. FCRM Note.reference_docname -> the row's reference_doctype (CRM Lead).
	dynamic_fields = {}  # fieldname -> options fieldname (the doctype selector)
	if doctype:
		for f in frappe.get_meta(doctype).fields:
			if f.fieldtype == "Dynamic Link" and f.options:
				dynamic_fields[f.fieldname] = f.options

	if not link_fields and not dynamic_fields:
		return

	titles = result.setdefault("_link_titles", {})

	def add(dt, value):
		# Dedup, then delegate to the ONE resolver. A Dynamic Link target can be a permissioned record,
		# so _resolve_title gates on read (a caller who sees the row but not the referenced lead must not
		# get the lead's name); static masters (Stage/Picklist) read freely.
		if not (dt and value):
			return
		key = f"{dt}::{value}"
		if key in titles:
			return
		title = _resolve_title(dt, value)
		if title is not None:
			titles[key] = title

	for row in rows:
		for fieldname, target_dt in link_fields.items():
			add(target_dt, row.get(fieldname))
		for fieldname, options_field in dynamic_fields.items():
			add(row.get(options_field), row.get(fieldname))


def _resolve_title(target_dt, value):
	"""The ONE Link title resolver: the target doctype's title_field for `value` when it opts into
	show_title_field_in_link (framework flag) AND the caller may read it — else None (no clean title).
	Shared by the list `_link_titles` map and the single-value `get_link_title` (detail / side panel),
	so every surface strips the composite `::` PK the same way, from the same source."""
	if not (target_dt and value):
		return None
	try:
		meta = frappe.get_meta(target_dt)
	except Exception:
		return None
	if not (meta.show_title_field_in_link and meta.title_field):
		return None
	if not frappe.has_permission(target_dt, "read", doc=value):
		return None
	return frappe.get_cached_value(target_dt, value, meta.title_field) or value


@frappe.whitelist()
def get_doc_link_titles(doctype, name):
	"""The `_link_titles` map ({target_doctype}::{value} -> clean title) for ONE document's Link /
	Dynamic Link fields — the detail / side-panel counterpart of the list `get_data` map, same brain
	(`_resolve_title`). ONE call per doc load (mirrors the assignees / permissions resources in
	`useDocument`), never per field. Only fields whose target opts into show_title_field_in_link get an
	entry; the UI falls back to the raw value for the rest, so it never blanks."""
	frappe.has_permission(doctype, "read", doc=name, throw=True)
	doc = frappe.get_cached_doc(doctype, name)
	titles = {}
	for df in frappe.get_meta(doctype).fields:
		if df.fieldtype not in ("Link", "Dynamic Link"):
			continue
		value = doc.get(df.fieldname)
		target = df.options if df.fieldtype == "Link" else doc.get(df.options)
		title = _resolve_title(target, value)
		if title is not None:
			titles[f"{target}::{value}"] = title
	return titles
