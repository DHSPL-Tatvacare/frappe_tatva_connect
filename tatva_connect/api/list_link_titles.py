"""TATVA override of `crm.api.doc.get_data`.

Attaches the framework's `_link_titles` map ({target_doctype}::{value} -> title) for every Link field
whose target declares `show_title_field_in_link`. The list, Kanban and group-by surfaces are all fed
by this one method, so the map rides everywhere: the cell shows the target's `title_field` in place of
the composite `::` primary key, and the row keeps the key, which is what the client filters, sorts and
groups by. Resolving the title INTO the row instead would break all three.

Two sources feed the map. Row values cover the list and group-by. Kanban columns do not: the board's
columns are the group field's master rows, so a column's `name` is itself a primary key and never
appears in `data`. `_add_kanban_columns` titles those.

One generic place, driven by the framework's own flag, no per-field or per-surface code. Delegates to
the unchanged native `get_data` (same pattern as `access/native_guards`), and the title_field read
itself lives once, in `taxonomy.labels.title_of`.
"""

import frappe

from tatva_connect.storage import file_names
from tatva_connect.taxonomy import labels


@frappe.whitelist()
def get_data(**kwargs):
	result = _native_get_data(**kwargs)
	if isinstance(result, dict):
		_attach_link_titles(result, kwargs.get("doctype"))
	return result


def _native_get_data(**kwargs):
	# The list engine, which delegates to the unchanged native `get_data` for every doctype that declares
	# no derived field — so this stays the one door and the inert path is byte-identical.
	from tatva_connect.list_engine import engine

	return engine.get_data(**kwargs)


def _attach_link_titles(result, doctype=None):
	rows = result.get("data") or []

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

	# Kanban columns are the group field's master rows, not row values, so they are not reachable from
	# `data`. Each column is titled by its own `name`, which for a grain master is the composite key.
	_add_kanban_columns(result, doctype, add)

	for row in rows:
		for fieldname, target_dt in link_fields.items():
			add(target_dt, row.get(fieldname))
		for fieldname, options_field in dynamic_fields.items():
			add(row.get(options_field), row.get(fieldname))


def _add_kanban_columns(result, doctype, add):
	"""Title every Kanban column. The board groups by a Link field, so a column's `name` is the target's
	primary key: on the Lead board by stage that is `Sigrima::Treatment on Hold`, printed as the column
	header. The rows the board holds are titled by the caller's normal Link-field pass."""
	columns = result.get("kanban_columns") or []
	column_field = result.get("column_field")
	if not (columns and column_field and doctype):
		return
	df = frappe.get_meta(doctype).get_field(column_field)
	if not df or df.fieldtype != "Link" or not df.options:
		return
	for column in columns:
		add(df.options, column.get("name"))


def _resolve_title(target_dt, value):
	"""The title for a `_link_titles` entry: the framework's two gates (the target opts in via
	show_title_field_in_link, and the caller may read it), then the shared lookup. The title_field read
	itself lives in taxonomy.labels.title_of, so there is one implementation of it, not two."""
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
	return labels.title_of(target_dt, value) or value


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
	_add_attach_labels(doc, doctype, titles)
	return titles


ATTACH_FIELDTYPES = ("Attach", "Attach Image")


def _add_attach_labels(doc, doctype, titles):
	"""An Attach value IS a file_url, and the storage key inside it is slugged, so a control rendering the
	value raw shows `urmila_doc.jpeg` for a file the user named `URMILA DOC.jpeg`. The real name rides in
	the same map under `File::<url>`, resolved by the one utility, so the control reads and never derives.

	Child rows are walked too: a grid cell is an Attach control like any other, and its value never
	appears on the parent's own fields."""
	urls = []
	for df in frappe.get_meta(doctype).fields:
		if df.fieldtype in ATTACH_FIELDTYPES:
			urls.append(doc.get(df.fieldname))
		elif df.fieldtype == "Table" and df.options:
			urls.extend(_child_attach_urls(doc, df))
	for url, label in file_names.display_names(urls).items():
		titles[f"File::{url}"] = label


def _child_attach_urls(doc, df):
	"""Every Attach value held by one Table field's rows."""
	fieldnames = [c.fieldname for c in frappe.get_meta(df.options).fields
				  if c.fieldtype in ATTACH_FIELDTYPES]
	if not fieldnames:
		return []
	return [row.get(f) for row in (doc.get(df.fieldname) or []) for f in fieldnames]
