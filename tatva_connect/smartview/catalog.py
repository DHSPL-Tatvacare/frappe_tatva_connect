"""Smart Views field catalog (catalog -> query -> api): the resolved field set is the allowlist no other module may widen."""
import frappe
from frappe import _
from frappe.query_builder import DocType

from tatva_connect.access import entitlement
from tatva_connect.activity import api as activity_brain
from tatva_connect.lead import field_value
from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

LEAD_DOCTYPE = "CRM Lead"
# Every lead view leads with the row's own ID, whatever the caller's grain grants — the row already carries `name`.
LEAD_ID = "name"
TASK_DOCTYPE = "CRM Task"
# An activity field kept on the task row itself, beside the lead's own storage words in `field_value`.
TASK = "task"
_NO_JOIN_SOURCES = (field_value.PARENT, TASK)  # sql_source values answered off the driving row, no join



# Catalog read: Lead views from CRM Lead API Field + Section, Activity views from the activity brain live; grain- and role-filtered here.

def _section_rows(doctype):
	"""A section doctype's rows keyed by section_key — the ONE row that owns a section's table, row key and answer column."""
	def build():
		return {
			r.name: r
			for r in frappe.get_all(
				doctype,
				fields=["name", "title", "target_doctype", "child_table_field", "is_multi_row", "row_key_field",
				        "is_key_value", "value_field"],
			)
		}

	return entitlement.request_cache("tatva_connect:smartview_sections", doctype, build)


def _sections():
	"""The lead sections."""
	return _section_rows("CRM Lead Section")


def _lead_catalog():
	"""Every lead catalog row by field_key, carrying its section's derived source and row-pick facts; request-cached."""
	def build():
		sections = _sections()
		rows = {}
		for r in frappe.get_all(
			"CRM Lead API Field",
			fields=[
				"field_key", "label", "fieldname", "section", "filterable", "sortable", "surface", "is_multi_value",
			],
			order_by="field_key asc",
		):
			section = sections.get(r.section)
			if not section:
				continue  # a row whose section does not resolve names no table to be read from
			kind = field_value.kind_of(section, r)
			if not field_value.on_page(kind):
				continue  # a virtual or unresolvable field has nothing a page can select or read
			r.sql_source = kind
			if kind == field_value.MULTI_VALUE:
				# Selections are read for the page, never compared in SQL; a grid cell reads them as one joined answer.
				r.filterable, r.sortable, r.fieldtype, r.options = 0, 0, "Small Text", ""
			r.row_key_field = section.row_key_field or ""  # the field a multi-row child is ordered by; blank -> creation
			r.value_field = section.value_field or ""  # the column a key-value row's answer is read from
			r.target_doctype = section.target_doctype
			r.section_title = section.title  # composed into the flat picker/grid label; the Data tab reads the plain label under its own section header
			rows[r.field_key] = r
		return rows

	return entitlement.request_cache("tatva_connect:smartview_catalog", "all", build)


def _task_sections():
	"""The activity sections."""
	return _section_rows("CRM Task Section")


def activity_key(fieldname):
	"""The column key an activity field is offered under in an Activity view."""
	return f"activity:{fieldname}"


def _activity_catalog(activity_type):
	"""The activity type's fields asked of the brain, keyed `activity:<fieldname>`, placed by `field_target` and compared in their typed column (D17)."""
	if not activity_type:
		return {}
	sections = _task_sections()
	rows = {}
	for f in activity_brain.get_schema(activity_type):
		section_key, address = activity_brain.field_target(f)
		section = sections.get(section_key)
		key = activity_key(f["fieldname"])
		value_field = (section.value_field or "") if section else ""
		rows[key] = frappe._dict(
			field_key=key,
			label=f["label"] or f["fieldname"],
			fieldname=address,
			# The shape classifier is a fact about a section's columns, not about which resource declared it.
			sql_source=crm_lead_section.sql_source(section) if section else TASK,
			row_key_field=(section.row_key_field or "") if section else "",
			value_field=value_field,
			compare_field=(activity_brain.typed_column(f["fieldtype"]) or value_field),
			target_doctype=section.target_doctype if section else TASK_DOCTYPE,
			filterable=1,
			sortable=1,
			surface="worklist",
			fieldtype=f["fieldtype"],
			options=f["options"],
		)
	return rows


def _answer_catalog():
	"""Key-value questions read from the answer data (declared nowhere else) as columns; request-cached only, so a new question appears at once."""
	return entitlement.request_cache("tatva_connect:smartview_answers", "all", _build_answer_catalog)


def _build_answer_catalog():
	"""The scan itself — one pass per key-value section, grouped by the question."""
	rows = {}
	for section in frappe.get_all(
		"CRM Lead Section",
		filters={"is_key_value": 1},
		fields=["name", "target_doctype", *crm_lead_section.COLUMN_FIELDS],
	):
		for q in frappe.get_all(
			section.target_doctype,
			# Scoped to CRM Lead parents, as `_joins` and `_hydrate` are, so a reused table leaks no other parent's questions.
			filters={section.row_key_field: ("is", "set"), "parenttype": LEAD_DOCTYPE},
			fields=[f"{section.row_key_field} as identity", f"{section.label_field} as label",
			        f"{section.question_field} as question"],
			group_by=section.row_key_field,
		):
			key = f"{section.name}:{q.identity}"
			rows[key] = frappe._dict(
				field_key=key,
				label=q.label or q.question or q.identity,
				fieldname=q.identity,
				sql_source=field_value.ANSWER,
				row_key_field=section.row_key_field,
				value_field=section.value_field,
				target_doctype=section.target_doctype,
				filterable=1,
				sortable=1,
				surface="worklist",
				fieldtype="Data",
				options="",
			)
	return rows


def _catalog_fields(base_object, activity_type, grains, roles):
	"""The allowlist for a view scope, keyed by field_key: grain-visible, role-restricted; an Activity view gets only its type's schema."""
	if base_object == "Activity":
		return entitlement.restrict_fields(_activity_catalog(activity_type), roles)
	# Screening answers join after entitlement: nothing declares them, and row visibility still bounds them (ADR 0005).
	return {**entitlement.resolve_fields(_lead_catalog(), grains, roles), **_answer_catalog(), **_lead_id_field()}


def _lead_id_field():
	"""The Lead ID catalog row, granted to every caller: every lead row already carries its own `name`."""
	return {k: r for k, r in _lead_catalog().items() if r.fieldname == LEAD_ID and r.sql_source in _NO_JOIN_SOURCES}


# `_identity_key` (guessed the chip column) archived in .archive/smartview-lead-id-pinned-2026-09-17: the pinned Lead ID is the chip.


def _grains_for_view(v):
	"""The grains a saved view resolves fields against: the caller's entitlement narrowed to the view's scope; never throws."""
	return entitlement.entitled_grains_within((v.vertical, v.group, v.program))


def _settle_grain(vertical, group, program):
	"""The one grain a saved view is scoped to: as given, stamped for a one-grain caller, open for ALL_GRAINS, else required."""
	if vertical or group or program:
		return vertical, group, program
	entitled = entitlement.entitled_grains()
	if entitled == entitlement.ALL_GRAINS:
		return vertical, group, program
	if len(entitled) == 1:
		only = next(iter(entitled))
		return (only[0] or None, only[1] or None, only[2] or None)
	frappe.throw(
		_("Choose the grain this view is for. Its columns are one grain's columns, so a view spanning several cannot say what a row means."),
		title=_("Grain required"),
	)


def _grains_from_axes(vertical, group, program):
	"""A one-grain set from explicit axes (fail-closed if not entitled), or the caller's entitled grains when none given."""
	if vertical or group or program:
		grain = (vertical or "", group or "", program or "")
		if not entitlement.grain_entitled(grain):
			frappe.throw(_("You are not entitled to this grain."), frappe.PermissionError)
		return {grain}
	return entitlement.entitled_grains()


def _flat_label(r):
	"""The label for a flat surface (picker, results grid) that has no section header to lean on: the section title prefixes the plain column label so a column reads unambiguously. The stored label stays the plain column name; activity fields (no section) render as-is."""
	base = r.label or r.fieldname
	return f"{r.section_title} — {r.label}" if r.get("section_title") and r.label else base


# Query assembly.

def _driving(base_object):
	"""(driving DocType name, driving qb table)."""
	return (LEAD_DOCTYPE, DocType(LEAD_DOCTYPE)) if base_object == "Lead" else (TASK_DOCTYPE, DocType(TASK_DOCTYPE))


def _starter_columns(cat):
	"""What a view projects when it chose nothing: the driving row's own worklist fields, never a join."""
	return [
		k for k, r in cat.items()
		if (r.surface or "worklist") == "worklist" and r.sql_source in _NO_JOIN_SOURCES
	]


# Always-shown columns: the Lead chip (the ID, drawn as the lead's title), the number a rep dials, and who answers for the lead.
_ALWAYS_SHOWN = {"Lead": (LEAD_ID, "mobile_no", "lead_owner")}


def _always_shown_fieldnames(base_object):
	"""The fieldnames every view of this base object carries."""
	return _ALWAYS_SHOWN.get(base_object, ())


def _always_shown(base_object, cat):
	"""The always-shown keys this caller's catalog carries on the driving row, in declared order; a withheld one is dropped, never forced."""
	keys = []
	for fieldname in _always_shown_fieldnames(base_object):
		for key, r in cat.items():
			if r.fieldname == fieldname and r.sql_source in _NO_JOIN_SOURCES and key not in keys:
				keys.append(key)
				break
	return tuple(keys)


def _with_always_shown(keys, base_object, cat):
	"""`keys` with the always-shown columns in front, deduped, order otherwise preserved."""
	always = _always_shown(base_object, cat)
	rest = [k for k in keys if k not in always]
	return [*always, *rest]


def _saved_json(doc, fieldname, default):
	"""A view's stored JSON field, or `default` when blank; corrupt JSON is logged and read as `default`, never thrown."""
	try:
		return frappe.parse_json(doc.get(fieldname)) if doc.get(fieldname) else default
	except Exception:
		frappe.write_only()(frappe.log_error)(title=f"smartview: corrupt {fieldname} JSON", message=f"view={doc.get('name')}")
		return default


def _column_field_keys(view, cat):
	"""The field_keys a view projects: always-shown first, then its catalog-bounded saved list or the starter set."""
	keys = [k for k in (_saved_json(view, "columns", []) or []) if k in cat]
	return _with_always_shown(keys or _starter_columns(cat), view.base_object, cat)


def _search_keys(cat, col_keys, driving_name):
	"""Filterable fields a free-text search compares: every projected column plus the driving row's derived identity fields."""
	from tatva_connect.search.index import IDENTIFIERS

	meta = frappe.get_meta(driving_name)
	identity = {*meta.get_search_fields(), meta.get_title_field()}
	identity |= {fieldname for _column, fieldname, _kind in IDENTIFIERS}
	return {
		key for key, r in cat.items()
		if r.filterable
		and (key in col_keys or (r.sql_source in _NO_JOIN_SOURCES and r.fieldname in identity))
	}


def _col_docfield(r):
	"""The live DocField backing a catalog row, standard fields included; None when it cannot be resolved."""
	dt = (r.target_doctype or "").strip()
	if not dt:
		return None
	# A key-value row's type is its section's value column, since its `fieldname` addresses a row.
	fieldname = r.value_field if r.sql_source == field_value.ANSWER else r.fieldname
	try:
		return crm_lead_section.docfield(dt, fieldname)
	except Exception:
		frappe.write_only()(frappe.log_error)(title="smartview: unresolvable catalog target",
		                                     message=f"{dt}.{fieldname}")
		return None


def _col_type(r):
	"""(fieldtype, options) for a column: an activity field's declared schema type, else the live DocField, else ('Data', '')."""
	if r.get("fieldtype"):
		return r.fieldtype, (r.options or "")
	df = _col_docfield(r)
	return (df.fieldtype, df.options or "") if df else ("Data", "")


def _link_master(r):
	"""The Link target a catalog column points at (read off `_col_type`), or None where it is not a Link."""
	fieldtype, options = _col_type(r)
	return options if fieldtype == "Link" else None


def _validate_columns(columns, cat):
	"""The requested columns as an order-preserving list; throws on any key outside the catalog."""
	if isinstance(columns, str):
		columns = frappe.parse_json(columns) if columns else []
	columns = columns or []
	bad = [k for k in columns if k not in cat]
	if bad:
		frappe.throw(_("Unknown column field(s): {0}").format(", ".join(map(str, bad))))
	return list(columns)
