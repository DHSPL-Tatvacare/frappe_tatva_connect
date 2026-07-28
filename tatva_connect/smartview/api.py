"""Smart Views composer — the single READ-ONLY query path for the grain surface.

A Smart View is a saved tabbed list over leads (one row per patient) or activities (one
row per CRM Task of an activity type). Its rows come from ONE `frappe.qb` query that:

  1. drives off CRM Lead (lead view) or CRM Task WHERE custom_task_type = activity_type,
  2. LEFT JOINs only the child tables actually referenced by columns/predicate (single-row on
     parent=name, or ordered by the section's row_key_field) — leads and activities alike, each
     field resolved to its physical home by its own brain,
  3. ANDs the permission query conditions — ALWAYS, fail-closed, on list AND count,
  4. translates the saved predicate JSON tree into nested qb WHERE (catalog fields only),
  5. applies ad-hoc filters/search/sort (catalog-bounded) and paginates,
  6. returns {columns, rows, total} — total being the tab's PQC-scoped count.

The resolved field set is the allowlist: no fieldname outside it can ever reach the SQL. A lead view
resolves it from `CRM Lead API Field` + `CRM Lead Section`; an activity view asks the activity brain
for the type's schema. No raw string SQL is built here — the only raw fragment is the framework's own
PQC string, wrapped in a PseudoColumn.
"""
import re

import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.query_builder.functions import Count
from frappe.utils import cint, cstr
from pypika.analytics import RowNumber
from pypika.terms import Function, PseudoColumn

from tatva_connect.access import entitlement, visibility
from tatva_connect.activity import api as activity_brain
from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section
from tatva_connect.taxonomy import labels

LEAD_DOCTYPE = "CRM Lead"
TASK_DOCTYPE = "CRM Task"
PAGE_MAX = 200
PAGE_DEFAULT = 50
_NO_JOIN_SOURCES = ("parent", "task")  # sql_source values answered off the driving row, no join

# A stored column width, and the only shape one may take: digits, an optional decimal, then a css unit.
# It is written straight into a style attribute by the grid, so it is validated on the way IN.
_WIDTH = re.compile(r"^\d+(\.\d+)?(rem|px|em|ch|%)$")

# Operators a predicate/filter condition may use -> a qb criterion builder.
_OPS = {
	"=": lambda f, v: f == v,
	"!=": lambda f, v: f != v,
	">": lambda f, v: f > v,
	">=": lambda f, v: f >= v,
	"<": lambda f, v: f < v,
	"<=": lambda f, v: f <= v,
	"like": lambda f, v: f.like(f"%{v}%"),
	"not like": lambda f, v: f.not_like(f"%{v}%"),
	"in": lambda f, v: f.isin(v if isinstance(v, (list, tuple)) else [v]),
	"not in": lambda f, v: f.notin(v if isinstance(v, (list, tuple)) else [v]),
	"is set": lambda f, v: f.isnotnull(),
	"is not set": lambda f, v: f.isnull(),
	# `between` is what the control sends for EVERY date field by default (getDefaultOperator), and
	# `timespan` is its named-range sibling. Without them a date filter silently narrowed nothing.
	"between": lambda f, v: f.between(*_date_pair(v)),
	"timespan": lambda f, v: f.between(*_timespan_pair(v)),
}


def _date_pair(value):
	"""Inclusive bounds of a `between`, from either wire shape: a JSON list, or the comma string DateRangePicker emits."""
	if isinstance(value, str):
		value = [v.strip() for v in value.split(",")]
	if not (isinstance(value, (list, tuple)) and len(value) == 2 and all(v for v in value)):
		frappe.throw(_("A between filter needs two dates."))
	return value[0], value[1]


def _timespan_pair(value):
	"""A named timespan as its two bounds, resolved by frappe. The control's option values ARE frappe's
	own strings ("last week", "last month", ...), so no date arithmetic is written here."""
	from frappe.utils import get_timespan_date_range

	span = get_timespan_date_range(cstr(value).lower())
	if not span:
		frappe.throw(_("Unknown timespan {0}").format(value))
	return span


# ---------------------------------------------------------------------------
# Catalog read — the allowlist for a (base_object, activity_type) scope.
#
# TWO brains answer here, because this surface shows two resources and each owns its own fields:
#
#   Lead view      -> CRM Lead API Field + CRM Lead Section. The section states ONCE which table holds
#                     a field, whose columns it is and how one row is picked; a field row restates none
#                     of it. Both facts below are DERIVED, never read off the field row.
#   Activity view  -> the activity brain (tatva_connect.activity.api), asked live. CRM Task Type IS the
#                     contract — the grain is its key — and CRM Task Type Field is its schema.
#
# Smart Views used to read a frozen COPY of that schema out of the lead catalog, addressed by a Data
# field holding a task type's name as text. The types were re-keyed to composite grain PKs: every real
# Link cascaded, the text did not, and every Activity view resolved zero fields. Nothing is copied now.
#
# On THIS (internal) path the catalog is grain-filtered and role-restricted by the one entitlement
# brain (tatva_connect.access.entitlement); the partner path never does either.
# ---------------------------------------------------------------------------

def _sections():
	"""The lead sections, keyed by section_key — the ONE row that owns a section's table and row key."""
	def build():
		return {
			r.name: r
			for r in frappe.get_all(
				"CRM Lead Section",
				fields=["name", "title", "target_doctype", "child_table_field", "is_multi_row", "row_key_field",
				        "is_key_value", "value_field"],
			)
		}

	return entitlement.request_cache("tatva_connect:smartview_sections", "all", build)


def _lead_catalog():
	"""Every lead catalog row, keyed by field_key, each carrying its section's DERIVED facts: where the
	column physically lives (parent/child) and how one row of it is picked. A stored copy of either is
	what left 29 rows with a blank source and called multi-row Lab single-row, so neither is stored.

	Request-cached — one read per request, shared across every scope/grain resolution."""
	def build():
		sections = _sections()
		rows = {}
		for r in frappe.get_all(
			"CRM Lead API Field",
			fields=[
				"field_key", "label", "fieldname", "section", "filterable", "sortable", "surface",
			],
			order_by="field_key asc",
		):
			section = sections.get(r.section)
			if not section:
				continue  # a row whose section does not resolve names no table to be read from
			r.sql_source = crm_lead_section.sql_source(section)
			r.row_key_field = section.row_key_field or ""  # the field a multi-row child is ordered by; blank -> creation
			r.value_field = section.value_field or ""  # the column a key-value row's answer is read from
			r.target_doctype = section.target_doctype
			r.section_title = section.title  # composed into the flat picker/grid label; the Data tab reads the plain label under its own section header
			rows[r.field_key] = r
		return rows

	return entitlement.request_cache("tatva_connect:smartview_catalog", "all", build)


def _task_sections():
	"""The activity sections, keyed by section_key — the ONE row that owns a section's table, its row key
	and the column an answer is read from. The task twin of `_sections`, and cached the same way."""
	def build():
		return {
			r.name: r
			for r in frappe.get_all(
				"CRM Task Section",
				fields=["name", "title", "target_doctype", "child_table_field", "is_multi_row",
						"row_key_field", "is_key_value", "value_field"],
			)
		}

	return entitlement.request_cache("tatva_connect:smartview_task_sections", "all", build)


def _activity_catalog(activity_type):
	"""The activity type's fields, ASKED of the brain. Keyed `activity:<fieldname>` — the schema field's
	own name, so re-keying the TYPE moves nothing here and the type itself is reached through the view's
	Link, which cascades.

	Where a field physically lives is `field_target`'s answer and never a second reading of it: a retained
	common CRM Task column is the driving row, anything else is the section row that addresses it. Every
	declared field is therefore a real column somewhere, so every one of them is filterable and sortable —
	the JSON payload, which could only ever be projected, is gone.

	D17: a key-value answer is READ from the column its section declares and COMPARED in the typed column
	the brain names for its declared fieldtype, so a date range is a date range and not a string range."""
	if not activity_type:
		return {}
	sections = _task_sections()
	rows = {}
	for f in activity_brain.get_schema(activity_type):
		section_key, address = activity_brain.field_target(f)
		section = sections.get(section_key)
		key = f"activity:{f['fieldname']}"
		value_field = (section.value_field or "") if section else ""
		rows[key] = frappe._dict(
			field_key=key,
			label=f["label"] or f["fieldname"],
			fieldname=address,
			# The shape classifier is a fact about a section's columns, not about which resource declared it.
			sql_source=crm_lead_section.sql_source(section) if section else "task",
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
	"""The questions a key-value section actually holds, offered as columns and filters.

	Read from the DATA rather than from a catalog, because a screening question is declared nowhere: what
	has been asked is the only true list, and it grows on its own as campaigns run. The digest is the
	fieldname, so the same question is one column across every form that asked it.

	Request-cached — one read per request, like every other catalog here."""
	def build():
		rows = {}
		for section in frappe.get_all(
			"CRM Lead Section",
			filters={"is_key_value": 1},
			fields=["name", "target_doctype", *crm_lead_section.COLUMN_FIELDS],
		):
			for q in frappe.get_all(
				section.target_doctype,
				filters={section.row_key_field: ("is", "set")},
				fields=[f"{section.row_key_field} as identity", f"{section.label_field} as label",
				        f"{section.question_field} as question"],
				group_by=section.row_key_field,
			):
				key = f"{section.name}:{q.identity}"
				rows[key] = frappe._dict(
					field_key=key,
					label=q.label or q.question or q.identity,
					fieldname=q.identity,
					sql_source="answer",
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

	return entitlement.request_cache("tatva_connect:smartview_answers", "all", build)


def _catalog_fields(base_object, activity_type, grains, roles):
	"""Catalog rows usable by a view of this base object/type, keyed by field_key, after entitlement:
	visible in `grains`, minus the fields restricted for `roles`, plus the universal floor. The composer
	never touches a fieldname outside this dict — so a saved view can never project a field outside its
	grain.

	An Activity view resolves the TYPE's schema and nothing else: its rows are CRM Tasks, so a CRM Lead
	column has no column on this query to come from. The type's key already carries its grain, so its
	fields need no second grain filter — resolve_fields still applies the role restrictions."""
	if base_object == "Activity":
		return entitlement.restrict_fields(_activity_catalog(activity_type), roles)
	# Screening answers are added AFTER entitlement, deliberately: a question is declared nowhere, so
	# there is no catalog row to tick and nothing for resolve_fields to judge. Row visibility still
	# holds — an answer hangs off a lead, and a lead outside the caller's line is unreadable. ADR 0005.
	return {**entitlement.resolve_fields(_lead_catalog(), grains, roles), **_answer_catalog()}


def _grains_for_view(v):
	"""The grain a saved view resolves its fields against: its own stored (vertical, group,
	program) when set, else the caller's entitled grains (fail-closed fallback)."""
	return _grains_from_axes(v.vertical, v.group, v.program)


def _settle_grain(vertical, group, program):
	"""The grain a SAVED view is scoped to, decided once at save.

	A view's columns are a fixed set for the whole table, so they can only be one grain's columns:
	resolved against several, the table carries grain A's columns beside grain B's leads, blank on most
	rows and contradicting the rule the Data Tab keeps. `_grains_from_axes` unions the caller's grains
	when none are named, which is right for the PICKER (offer what could be chosen) and wrong once the
	choice has been made.

	Hold one grain and there is nothing to choose, so it is stamped rather than demanded. Hold several
	and the view must say which. A System Manager holds ALL_GRAINS, whose whole meaning is the entire
	catalog, so an open view is theirs to make."""
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
	"""A one-grain set from explicit axes, or the caller's entitled grains when none given
	(editor with no grain chosen yet → show what the caller could pick). Explicit axes are
	CLAMPED to entitlement: a grain the caller isn't entitled to is rejected fail-closed, so a
	client cannot widen scope past entitled_grains() on the picker, write, or data path."""
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


@frappe.whitelist()
def field_catalog(base_object, activity_type=None, vertical=None, group=None, program=None):
	"""The allowed fields for the picker/condition builder — type + grain scoped, role-restricted.
	The single source the editor (P2) and the composer agree on. The editor passes its selected
	grain (vertical/group/program); with none chosen the caller's entitled grains apply."""
	if base_object not in ("Lead", "Activity"):
		frappe.throw(_("Unknown base object {0}").format(base_object))
	grains = _grains_from_axes(vertical, group, program)
	cat = _catalog_fields(base_object, activity_type, grains, frappe.get_roles())
	out = []
	for r in cat.values():
		fieldtype, options = _col_type(r)
		out.append({
			"field_key": r.field_key,
			"label": _flat_label(r),
			"fieldname": r.fieldname,
			"sql_source": r.sql_source,
			"filterable": bool(r.filterable),
			"sortable": bool(r.sortable),
			"surface": r.surface or "worklist",
			"fieldtype": fieldtype,
			"options": options,
		})
	return out


# ---------------------------------------------------------------------------
# Tabs + sidebar gate — the READ-ONLY surface the SPA boots from.
# get_smart_views feeds the store (the row of tabs); access() is the Near Me-style
# fail-closed sidebar gate. Neither writes; neither ever throws on the happy path.
# ---------------------------------------------------------------------------

def _can_write_view(d):
	"""Whether the caller may edit/delete this view: a standard view is operator-only; a personal
	view is editable by its owner (or an operator). Drives the tab's edit affordance — the server
	upsert/delete still enforces the same rule, so the UI flag is convenience, never the gate."""
	if _is_operator():
		return True
	if d.get("is_standard"):
		return False
	return (d.get("owner_user") or None) == frappe.session.user


def _smart_view_tab(d):
	"""One tab row for the frontend store — the minimal shape SmartViewTabs renders."""
	return {
		"name": d.name,
		"label": d.label,
		"base_object": d.base_object,
		# The PK stays: the view filters on it. The label sits beside it, for display.
		"activity_type": d.activity_type,
		"activity_type_label": labels.label(d.activity_type, labels.TASK_TYPE),
		"color": d.color,
		"icon": d.icon,
		"order": cint(d.view_order),
		"pinned": bool(d.pinned),
		# Presentation only — the grid applies it on its first paint so a remembered width never jumps.
		"column_widths": frappe.parse_json(d.column_widths) if d.get("column_widths") else {},
		"is_standard": bool(d.get("is_standard")),
		"can_write": _can_write_view(d),
	}


@frappe.whitelist()
def get_smart_views():
	"""The caller's tabs: every standard (grain-shared) view + the caller's own, ordered.
	P1 keeps curation simple — all is_standard=1 views (grain-filtering is P3). Read-only;
	returns [] (never throws) when nothing is seeded, so the surface degrades gracefully."""
	user = frappe.session.user
	rows = frappe.get_all(
		"CRM Smart View",
		or_filters={"is_standard": 1, "owner_user": user},
		fields=[
			"name", "label", "base_object", "activity_type",
			"color", "icon", "view_order", "pinned", "is_standard", "owner_user",
			# Presentation only, and it rides HERE because the list applies it on its FIRST paint —
			# fetching it separately would mean the grid renders at default widths and then jumps.
			"column_widths",
		],
		order_by="view_order asc, label asc",
	)
	return [_smart_view_tab(frappe._dict(r)) for r in rows]


@frappe.whitelist()
def get_view(name):
	"""The full editable definition of one view (for the authoring editor): scope + parsed
	predicate + column keys. Readable when it's a standard view, the caller's own, or by an
	operator; otherwise refused (fail-closed). Read-only."""
	d = frappe.get_doc("CRM Smart View", name)
	if not (d.is_standard or _is_operator() or (d.owner_user and d.owner_user == frappe.session.user)):
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	try:
		predicate = frappe.parse_json(d.predicate) if d.predicate else None
	except Exception:
		frappe.log_error(title="smartview: corrupt predicate JSON", message=f"view={d.name}")
		predicate = None
	try:
		columns = frappe.parse_json(d.columns) if d.columns else []
	except Exception:
		frappe.log_error(title="smartview: corrupt columns JSON", message=f"view={d.name}")
		columns = []
	return {
		"name": d.name,
		"label": d.label,
		"base_object": d.base_object,
		"activity_type": d.activity_type,
		"activity_type_label": labels.label(d.activity_type, labels.TASK_TYPE),
		"vertical": d.vertical,
		"group": d.group,
		"program": d.program,
		"description": d.description,
		"color": d.color,
		"icon": d.icon,
		"predicate": predicate,
		"columns": columns,
		"column_widths": frappe.parse_json(d.column_widths) if d.column_widths else {},
		"is_standard": bool(d.is_standard),
		"can_write": _can_write_view(d),
	}


@frappe.whitelist()
def access():
	"""The sidebar gate (Near Me pattern). Fail-closed: visible only when the caller has at
	least one Smart View to land on (a standard view or one of their own). Never throws — any
	error reads as not-visible, so stock CRM shows no link when the surface is empty/absent."""
	try:
		user = frappe.session.user
		if user in ("Guest", "Administrator"):
			visible = frappe.db.count("CRM Smart View", {"is_standard": 1}) > 0 if user == "Administrator" else False
		else:
			visible = bool(
				frappe.db.exists("CRM Smart View", {"is_standard": 1})
				or frappe.db.exists("CRM Smart View", {"owner_user": user})
			)
	except Exception:
		frappe.log_error(title="smartview: access gate check failed")
		visible = False
	return {"visible": visible}


# ---------------------------------------------------------------------------
# Query assembly.
# ---------------------------------------------------------------------------

def _driving(base_object):
	"""(driving DocType name, driving qb table)."""
	return (LEAD_DOCTYPE, DocType(LEAD_DOCTYPE)) if base_object == "Lead" else (TASK_DOCTYPE, DocType(TASK_DOCTYPE))


def _starter_columns(cat):
	"""What a view projects when it has chosen nothing — the driving row's OWN fields, never a join.

	Stock CRM declares a default column set per doctype (`default_list_data`); this is the same idea,
	read off the brain instead of hardcoded. Child and answer fields are excluded by construction, and
	that is what keeps a column-less view under MariaDB's 61-table join ceiling however many screening
	questions exist. `empty` therefore means this set — it has never meant "every field"."""
	return [
		k for k, r in cat.items()
		if (r.surface or "worklist") == "worklist" and r.sql_source in _NO_JOIN_SOURCES
	]


def _column_field_keys(view, cat):
	"""The catalog field_keys this view projects. A saved list is used as-is (catalog-bounded);
	a view carrying none falls to the starter set."""
	try:
		keys = frappe.parse_json(view.columns) if view.columns else []
	except Exception:
		frappe.log_error(title="smartview: bad saved columns JSON")
		keys = []
	keys = [k for k in (keys or []) if k in cat]
	return keys or _starter_columns(cat)


def _predicate_keys(node, acc):
	"""Collect every field_key referenced anywhere in the predicate tree (so we join
	only the children a condition actually needs)."""
	if not isinstance(node, dict):
		return
	if "conditions" in node:
		for c in node.get("conditions") or []:
			_predicate_keys(c, acc)
	elif node.get("field"):
		acc.add(node["field"])


def _joins(needed_keys, cat, driving_table, driving_name):
	"""LEFT JOIN every child table referenced by `needed_keys`, once per (doctype, order_field).
	Returns (query-mutator, {field_key: pypika Field}, {field_key: the Field a predicate compares}).
	No order_field -> join on parent=name + parenttype ordered by creation; a row_key_field -> a subquery
	picking the newest row per parent. The driving table's own (parent/task) fields resolve straight off
	driving_table.

	The two term maps differ for exactly one shape (D17): a key-value answer is PROJECTED from the column
	its section declares and COMPARED in the typed column the catalog names, so a Datetime answer filters
	and sorts as a date. Everywhere else the compared term is the projected one."""
	field_terms = {}
	compare_terms = {}  # only where a row is compared somewhere other than where it is read (D17)
	join_specs = {}  # alias -> (aliased child table, order_field, child doctype)
	answer_specs = {}  # alias -> the catalog row whose field this join answers
	for key in needed_keys:
		r = cat.get(key)
		if not r:
			continue
		if r.sql_source == "answer":
			# One row per field, so one join per field the view selects. The alias is positional
			# because a field_key is not a SQL identifier.
			alias = f"_tc_ans_{len(answer_specs)}"
			answer_specs[alias] = (key, r)
			aliased = DocType(r.target_doctype).as_(alias)
			field_terms[key] = aliased[r.value_field]
			compare_terms[key] = aliased[r.get("compare_field") or r.value_field]
			continue
		if r.sql_source in ("parent", "task"):
			field_terms[key] = driving_table[r.fieldname]
			continue
		# child (CRM Lead child table) -> needs a join
		child_dt = (r.target_doctype or "").strip()
		if not child_dt:
			continue
		order_field = (r.row_key_field or "creation").strip()  # multi-row child ordered by its row key; else creation
		alias = f"{child_dt}__{order_field}".replace(" ", "_")
		child_tbl = join_specs.get(alias, (None,))[0]
		if child_tbl is None:
			child_tbl = DocType(child_dt).as_(alias)
			join_specs[alias] = (child_tbl, order_field, child_dt)
		# A real Field off the aliased child table -> .as_(field_key) aliases correctly,
		# so the row dict is keyed by field_key (never the bare fieldname).
		field_terms[key] = child_tbl[r.fieldname]

	# the physical table backing the driving doctype (qb aliases tables as `tab<DocType>`).
	driving_tbl = f"tab{driving_name}"

	def apply(query):
		# One row per parent: the question's NEWEST answer, ordered by idx like `keyvalue.newest_first`.
		for spec_alias, (_spec_key, r) in answer_specs.items():
			inner = DocType(r.target_doctype)
			match = inner[r.row_key_field] == r.fieldname
			rn = RowNumber().over(inner.parent).orderby(inner.idx, order=frappe.qb.desc)
			ranked = (
				frappe.qb.from_(inner)
				.select(inner.star, rn.as_("_tc_rn"))
				.where((inner.parenttype == driving_name) & match)
			)
			sub = (
				frappe.qb.from_(ranked)
				.select(PseudoColumn("*"))
				.where(PseudoColumn("`_tc_rn` = 1"))
			).as_(spec_alias)
			query = query.left_join(sub).on(
				PseudoColumn(f"`{spec_alias}`.`parent` = `{driving_tbl}`.`name`")  # sqli-ok: join on constant/validated identifiers (alias + driving table/name), no user value
			)
		# Every child join yields ONE row per parent — the newest by its order field (blank -> creation).
		# ROW_NUMBER() OVER (PARTITION BY parent ORDER BY …), keep rn=1;
		# a plain join would multiply the parent for a multi-row child, inflating rows AND the count.
		for spec_alias, (_child_tbl, order_field, spec_child_dt) in join_specs.items():
			inner = DocType(spec_child_dt)
			rn = (
				RowNumber()
				.over(inner.parent)
				.orderby(inner[order_field], order=frappe.qb.desc)
				.orderby(inner.creation, order=frappe.qb.desc)
				.orderby(inner.name, order=frappe.qb.desc)
			)
			ranked = (
				frappe.qb.from_(inner)
				.select(inner.star, rn.as_("_tc_rn"))
				.where(inner.parenttype == driving_name)
			)
			sub = (
				frappe.qb.from_(ranked)
				.select(PseudoColumn("*"))
				.where(PseudoColumn("`_tc_rn` = 1"))
			).as_(spec_alias)
			query = query.left_join(sub).on(
				PseudoColumn(f"`{spec_alias}`.`parent` = `{driving_tbl}`.`name`")  # sqli-ok: join on constant/validated identifiers (alias + driving table/name), no user value
			)
		return query

	return apply, field_terms, {**field_terms, **compare_terms}


def _never_matches():
	"""A condition that selects nothing — how a saved view fails CLOSED when a field cannot be resolved.
	`1=0` is the same constant `access/visibility.py` uses to deny, wrapped the way this file already
	wraps the framework's own PQC fragment."""
	return PseudoColumn("1=0")  # sqli-ok: a constant, no user value reaches this string


def _criterion(field_term, op, value):
	builder = _OPS.get(op)
	if not builder:
		frappe.throw(_("Unsupported operator {0}").format(op))
	return builder(field_term, value)


def _predicate_where(node, cat, terms):
	"""Translate a predicate node -> a qb criterion (or None). A group has `op`
	(and/or) + `conditions`; a leaf has `field`/`operator`/`value`. Only catalog
	fields with `filterable` reach a clause. `terms` are the COMPARED terms (D17)."""
	if not isinstance(node, dict):
		return None
	if "conditions" in node:
		parts = [c for c in (_predicate_where(x, cat, terms) for x in node["conditions"]) if c is not None]
		if not parts:
			return None
		joiner = (node.get("op") or "and").lower()
		crit = parts[0]
		for p in parts[1:]:
			crit = (crit | p) if joiner == "or" else (crit & p)
		return crit
	key = node.get("field")
	r = cat.get(key)
	if not r or not r.filterable or key not in terms:
		# A SAVED predicate is the view's definition, so a condition that cannot be resolved narrows to
		# nothing rather than disappearing. Dropped, it widened the view instead: a filter on a question
		# no lead currently answers returned every lead, and one naming a field outside the caller's
		# grain returned more rows than the view was written to show. Ad-hoc filters stay tolerant.
		return _never_matches()
	return _criterion(terms[key], node.get("operator") or "=", node.get("value"))


def _apply_filters(crit, filters, cat, terms):
	"""Ad-hoc filters: [[field_key, op, value], ...], catalog + filterable bounded, compared on the
	COMPARED term (D17) so a date range is a date range.

	Honoured or refused, never ignored. Skipping one silently hands back a list that looks filtered and
	is not, which is worse than an error: the user reads it as the answer to a question it never asked.
	A saved predicate already fails CLOSED here (`_never_matches`) when a field cannot resolve; a filter
	the user set a second ago fails LOUD. Both refuse to guess."""
	for f in filters or []:
		if not (isinstance(f, (list, tuple)) and len(f) == 3):
			continue
		key, op, value = f
		r = cat.get(key)
		if not r or not r.filterable or key not in terms:
			frappe.throw(_("{0} cannot be filtered on here.").format(key))
		c = _criterion(terms[key], op, value)
		crit = c if crit is None else (crit & c)
	return crit


def _apply_search(crit, search, cat, field_terms):
	"""Free-text search across the projected filterable text fields (OR of LIKEs) — on the READ term, which
	is the text a user sees and therefore the text they are searching."""
	search = (search or "").strip()
	if not search:
		return crit
	likes = [
		field_terms[k].like(f"%{search}%")
		for k, r in cat.items()
		if r.filterable and k in field_terms
	]
	if not likes:
		return crit
	sc = likes[0]
	for l in likes[1:]:
		sc = sc | l
	return sc if crit is None else (crit & sc)


def _col_docfield(r):
	"""The live DocField backing a lead catalog row, read from doctype meta (never guessed). The row's
	target_doctype is its section's. None when it cannot be resolved."""
	dt = (r.target_doctype or "").strip()
	if not dt:
		return None
	# A key-value row's `fieldname` addresses a ROW, so the column its answer is read from is the
	# section's value column, and that is the type the worklist must format.
	fieldname = r.value_field if r.sql_source == "answer" else r.fieldname
	try:
		return frappe.get_meta(dt).get_field(fieldname)
	except Exception:
		return None


def _col_type(r):
	"""(fieldtype, options) for a catalog column — drives the frontend's column width, cell formatting
	and the native Filter/ColumnSettings controls (operator menu, value widget, Link target).

	An activity field answers with the SCHEMA's own type, because that is the type the brain declared and
	the one the form submits: the promoted column it lands in is a generic Data column, and reading meta
	there would flatten a Select back to free text. A lead field is a real column, so meta IS its truth.
	Unknown -> 'Data' (inert)."""
	if r.get("fieldtype"):
		return r.fieldtype, (r.options or "")
	df = _col_docfield(r)
	return (df.fieldtype, df.options or "") if df else ("Data", "")


@frappe.whitelist()
def get_data(view, filters=None, sort=None, search=None, columns=None, page=1, page_size=50):
	"""THE composer. Returns {columns, rows, total} for a saved CRM Smart View, PQC-scoped.
	Read-only. One qb query for the rows + one for the count; both AND the PQC.

	`columns` (optional) is an interactive, catalog-bounded override of the saved column set
	(the ColumnSettings picker) — a transient projection, never persisted by this read path."""
	v = frappe.get_doc("CRM Smart View", view)
	base_object = v.base_object
	activity_type = v.activity_type

	cat = _catalog_fields(base_object, activity_type, _grains_for_view(v), frappe.get_roles())
	driving_name, driving_table = _driving(base_object)

	col_keys = _column_field_keys(v, cat)
	# Interactive column override wins over the saved set, but stays catalog-bounded: an
	# A requested projection is validated against the SAME allowlist the save path uses: an unknown key
	# throws instead of being dropped, so a wrong client key surfaces on the first click, not months later.
	if columns is not None:
		req = _validate_columns(columns, cat)
		if req:
			col_keys = req
	try:
		predicate = frappe.parse_json(v.predicate) if v.predicate else None
	except Exception:
		frappe.log_error(title="smartview: bad predicate JSON")
		predicate = None
	if isinstance(filters, str):
		filters = frappe.parse_json(filters) or []

	# Only join children referenced by columns OR predicate (lean).
	needed = set(col_keys)
	_predicate_keys(predicate, needed)
	for f in filters or []:
		if isinstance(f, (list, tuple)) and len(f) == 3:
			needed.add(f[0])

	def scoped(keys):
		"""(apply_joins, field_terms, compare_terms, criterion) for ONE join set. The WHERE is rebuilt from that set's own
		terms because a term is a Field bound to an aliased table — a criterion built over one join set
		cannot be re-used over another, and an answer alias is positional. Same three functions, same
		catalog, asked once per set: no second query builder and no second filter rule."""
		apply_joins, field_terms, compare_terms = _joins(keys, cat, driving_table, driving_name)
		# WHERE: predicate tree + ad-hoc filters + search, ALL catalog-bounded.
		crit = _predicate_where(predicate, cat, compare_terms)
		crit = _apply_filters(crit, filters, cat, compare_terms)
		crit = _apply_search(crit, search, cat, field_terms)
		# Activity view: pin the task type (indexed). Lead view: no extra base filter.
		if base_object == "Activity" and activity_type:
			tc = driving_table.custom_task_type == activity_type
			crit = tc if crit is None else (crit & tc)
		pqc = visibility.readable_criterion(driving_name, driving_table)
		if pqc is not None:
			crit = pqc if crit is None else (crit & pqc)
		return apply_joins, field_terms, compare_terms, crit

	# THE COUNT DOES NOT PAY FOR PROJECTION. Every join this composer builds is a `ROW_NUMBER() … = 1`
	# sub-select, so it yields exactly ONE row per parent and cannot change how many parents match — a
	# join that exists only because a COLUMN projects it is pure cost in the count. Dropping those turned
	# a wide activity worklist from one full scan of the answer table per displayed column into one per
	# FILTERED column, on every page load. `test_the_count_does_not_pay_for_projection` locks the number.
	#
	# Search is the exception, and it is a real one: `_apply_search` ORs a LIKE across every projected
	# field, so when a term is present those joins genuinely sit in the WHERE and the count needs them all.
	filtered_keys = set()
	_predicate_keys(predicate, filtered_keys)
	for f in filters or []:
		if isinstance(f, (list, tuple)) and len(f) == 3:
			filtered_keys.add(f[0])

	# sort: [field_key, "asc"|"desc"], sortable + catalog bounded; default modified desc. Resolved HERE,
	# before the join sets are chosen, because a field the query ORDERS BY has to be in the query.
	if isinstance(sort, str):
		sort = frappe.parse_json(sort)
	sort_key = sort[0] if (isinstance(sort, (list, tuple)) and sort) else None
	if sort_key and cat.get(sort_key) and cat[sort_key].sortable:
		filtered_keys.add(sort_key)
	else:
		sort_key = None

	count_keys = needed if (search or "").strip() else filtered_keys

	# THE PAGE IS FETCHED, THEN FILLED IN. A key-value field is stored as a ROW, not a column, so the
	# query needs one join PER FIELD to spread six of them across one line — and each of those joins
	# ranks the whole table for the whole type, whether the page shows fifty rows or fifty thousand.
	# That is the cost that grows with the data.
	#
	# So a key-value field the view only DISPLAYS leaves the query entirely and is fetched afterwards for
	# the page's own rows (`_hydrate`). A field that is FILTERED, SORTED or SEARCHED on stays in the query
	# — you cannot page a list before you have narrowed it. Displayed-only is the common case and the
	# expensive one: six joins become one small read keyed on fifty parents.
	#
	# Only key-value fields move. A section of real columns is ONE join however many of its fields are
	# shown (D3), which is already cheap, and a multi-row section needs its "latest row" ranking to stay
	# in the query — moving those would buy nothing and would fork a rule that lives in one place.
	must_query = filtered_keys | (needed if (search or "").strip() else set())
	hydrate_keys = {
		k for k in col_keys
		if k not in must_query and cat.get(k) and cat[k].sql_source == "answer"
	}
	query_keys = needed - hydrate_keys

	# ---- count (PQC-scoped) -------------------------------------------------
	count_joins, _cf, _cc, count_crit = scoped(count_keys)
	count_q = count_joins(frappe.qb.from_(driving_table).select(Count("*").as_("total")))
	if count_crit is not None:
		count_q = count_q.where(count_crit)
	total = cint(count_q.run(as_dict=True)[0].get("total"))

	# ---- rows ---------------------------------------------------------------
	apply_joins, field_terms, compare_terms, crit = scoped(query_keys)
	select_terms = [driving_table.name.as_("name")] + [field_terms[k].as_(k) for k in col_keys if k in field_terms]
	rows_q = apply_joins(frappe.qb.from_(driving_table).select(*select_terms))
	if crit is not None:
		rows_q = rows_q.where(crit)

	if sort_key and sort_key in compare_terms:
		direction = frappe.qb.desc if (len(sort) > 1 and str(sort[1]).lower() == "desc") else frappe.qb.asc
		rows_q = rows_q.orderby(compare_terms[sort_key], order=direction)
	else:
		rows_q = rows_q.orderby(driving_table.modified, order=frappe.qb.desc)

	page = max(cint(page) or 1, 1)
	size = min(cint(page_size) or PAGE_DEFAULT, PAGE_MAX)
	rows_q = rows_q.limit(size).offset((page - 1) * size)
	rows = rows_q.run(as_dict=True)

	_hydrate(rows, hydrate_keys, cat, driving_name)

	columns = [
		{"key": k, "label": _flat_label(cat[k]), "fieldtype": _col_type(cat[k])[0]}
		for k in col_keys if k in field_terms or k in hydrate_keys
	]
	return {"columns": columns, "rows": rows, "total": total}


def _hydrate(rows, keys, cat, driving_name):
	"""Fill the page's key-value columns in ONE read per table, keyed on the page's own rows.

	This is the second half of "fetch the page, then fill it in" — the same shape an ORM's eager load
	takes (Rails `preload`, Django `prefetch_related`): one query for the page, one for its values,
	stitched in memory. It reads `parent IN (this page)`, so its cost is the PAGE's size and never the
	table's — fifty rows cost the same read whether the table holds twenty thousand answers or forty
	million. That is the whole reason this exists.

	`frappe.get_all`, not a hand-built query: the (parent, fieldname) index it seeks already exists for
	the form's own read, and the framework's own reader keeps this on the same permission and escaping
	path as every other read in the app.

	Every requested key is set on every row — a value that is absent lands as None rather than a missing
	key, so a card that binds the column renders blank instead of breaking.
	"""
	if not rows or not keys:
		return
	# Keyed as TEXT on both sides: a driving row's `name` can come back as an int (CRM Task names are
	# numeric) while a child's `parent` is always a varchar, and an int key never matches a string one —
	# the columns silently stayed blank until this was normalised.
	names = [cstr(r["name"]) for r in rows]
	by_name = {cstr(r["name"]): r for r in rows}
	for key in keys:
		for r in rows:
			r.setdefault(key, None)

	# One read per (table, address column, value column) — in practice one, since a resource declares a
	# single key-value section. Grouped so a second one would cost a second read and not a second rule.
	buckets = {}
	for key in keys:
		row = cat[key]
		buckets.setdefault((row.target_doctype, row.row_key_field, row.value_field), {})[row.fieldname] = key

	for (doctype, address, value_field), fields in buckets.items():
		if not (doctype and address and value_field):
			continue
		for answer in frappe.get_all(  # authz-ok: tier-a — the page's rows already passed the composer's PQC
			doctype,
			filters={"parent": ["in", names], "parenttype": driving_name,
			         address: ["in", list(fields)]},
			fields=["parent", address, value_field],
			limit_page_length=0,
		):
			target = by_name.get(cstr(answer.get("parent")))
			key = fields.get(answer.get(address))
			if target is not None and key:
				target[key] = answer.get(value_field)


# ---------------------------------------------------------------------------
# Authoring (P2) — the ONLY write path. Owner-scoped + catalog-validated.
# Every field_key in a predicate or column set must be a catalog row for the view's
# scope (fail-closed allowlist); standard (grain-shared) views are operator-only;
# a non-operator owns at most OWNER_VIEW_CAP personal views. Writes run our own
# validation then save with ignore_permissions — the whitelisted method IS the gate
# (invariant: server-scoped writes), so the doctype stays System-Manager-only in Desk.
# ---------------------------------------------------------------------------

OWNER_VIEW_CAP = 20


def _is_operator():
	return "System Manager" in frappe.get_roles()


def _validate_columns(columns, cat):
	"""The requested columns, every one a catalog field_key for the scope. Throws on any
	unknown key (fail-closed allowlist). Returns the cleaned, order-preserving list."""
	if isinstance(columns, str):
		columns = frappe.parse_json(columns) if columns else []
	columns = columns or []
	bad = [k for k in columns if k not in cat]
	if bad:
		frappe.throw(_("Unknown column field(s): {0}").format(", ".join(map(str, bad))))
	return list(columns)


def _validate_predicate(node, cat):
	"""Walk the predicate tree; every leaf field must be a filterable catalog row and every
	operator one we support. Throws on violation (fail-closed). No SQL is built here — this
	only gates what may later reach _predicate_where."""
	if not isinstance(node, dict):
		return
	if "conditions" in node:
		for c in node.get("conditions") or []:
			_validate_predicate(c, cat)
		return
	key = node.get("field")
	if not key:
		return
	r = cat.get(key)
	if not r or not r.filterable:
		frappe.throw(_("Field {0} is not a filterable catalog field for this view.").format(key))
	if (node.get("operator") or "=") not in _OPS:
		frappe.throw(_("Unsupported operator {0}").format(node.get("operator")))


@frappe.whitelist()
def upsert_view(view):
	"""Create or update a Smart View — owner-scoped, catalog-validated. `view` is a dict (or
	JSON string): {name?, label, base_object, activity_type?, predicate?, columns?, description?,
	color?, icon?, view_order?, is_standard?, pinned?}. Returns the saved tab shape."""
	if isinstance(view, str):
		view = frappe.parse_json(view)
	if not isinstance(view, dict):
		frappe.throw(_("Invalid view payload."))

	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Not permitted."), frappe.PermissionError)

	base_object = view.get("base_object")
	if base_object not in ("Lead", "Activity"):
		frappe.throw(_("Unknown base object {0}").format(base_object))
	label = (view.get("label") or "").strip()
	if not label:
		frappe.throw(_("A view name is required."))

	activity_type = view.get("activity_type") or None
	if base_object == "Activity":
		if not activity_type:
			frappe.throw(_("An activity type is required for an Activity view."))
		if not frappe.db.exists("CRM Task Type", activity_type):
			frappe.throw(_("Unknown activity type {0}").format(activity_type))
	else:
		activity_type = None

	# The view's chosen grain bounds its catalog: columns/predicate are validated against exactly
	# the fields visible in that grain, so a saved view can never carry an out-of-grain field.
	vertical = view.get("vertical") or None
	group = view.get("group") or None
	program = view.get("program") or None
	vertical, group, program = _settle_grain(vertical, group, program)
	grains = _grains_from_axes(vertical, group, program)
	cat = _catalog_fields(base_object, activity_type, grains, frappe.get_roles())
	# Materialised on save, so a view's projection is always an explicit stored list. What "empty" meant
	# then stops depending on what the catalog happens to hold later.
	columns = _validate_columns(view.get("columns"), cat) or _starter_columns(cat)
	predicate = view.get("predicate")
	if isinstance(predicate, str):
		predicate = frappe.parse_json(predicate) if predicate else None
	if predicate:
		_validate_predicate(predicate, cat)

	operator = _is_operator()
	name = view.get("name")
	if name:
		doc = frappe.get_doc("CRM Smart View", cstr(name))
		# A standard view, or any view you don't own, is operator-only to edit.
		if (doc.is_standard or (doc.owner_user and doc.owner_user != user)) and not operator:
			frappe.throw(_("You can only edit your own views."), frappe.PermissionError)
	else:
		# Create: enforce the per-owner cap on a non-operator's personal views.
		if not (view.get("is_standard") and operator):
			if frappe.db.count("CRM Smart View", {"owner_user": user, "is_standard": 0}) >= OWNER_VIEW_CAP:
				frappe.throw(_("You have reached the limit of {0} views.").format(OWNER_VIEW_CAP))
		doc = frappe.new_doc("CRM Smart View")

	# Standard (grain-shared) views are operator-only; everyone else writes a view they own.
	is_standard = 1 if (view.get("is_standard") and operator) else 0
	doc.label = label
	doc.base_object = base_object
	doc.activity_type = activity_type
	doc.vertical = vertical
	doc.group = group
	doc.program = program
	doc.is_standard = is_standard
	doc.owner_user = None if is_standard else user
	doc.predicate = frappe.as_json(predicate) if predicate else None
	doc.columns = frappe.as_json(columns) if columns else None
	doc.description = view.get("description") or None
	doc.color = view.get("color") or None
	doc.icon = view.get("icon") or None
	doc.pinned = 1 if view.get("pinned") else 0
	if view.get("view_order") is not None:
		doc.view_order = cint(view.get("view_order"))
	doc.save(ignore_permissions=True)  # authz-ok: tier-a — smart-view scaffolding, operator-run
	return _smart_view_tab(doc)


@frappe.whitelist()
def set_column_widths(view, widths):
	"""Remember what a rep dragged the grid to. Presentation only — it touches no query.

	Its own endpoint rather than a field on `upsert_view`, because dragging a column is not authoring a
	view: it must not re-validate a predicate, must not re-check a column set, and must not fail because
	the saved definition has drifted. It writes ONE field.

	Gated by the SAME rule the rest of the write path uses (`_can_write_view`) — a standard view is
	operator-only, a personal view is its owner's — and a caller who may not write simply keeps the width
	for their session rather than being shown an error for dragging a column.

	`db.set_value`, not `save()`: this is a presentation preference, so it must not bump `modified` (which
	would make every drag look like an edit in the audit trail) and must not fire the doctype's validate.
	"""
	d = frappe.get_doc("CRM Smart View", view)
	if not _can_write_view(d):
		return {"saved": False}
	widths = frappe.parse_json(widths) if isinstance(widths, str) else (widths or {})
	if not isinstance(widths, dict):
		frappe.throw(_("Column widths must be an object of {field_key: width}."))
	# Bounded and sanitised: only keys this view actually projects, and only a plain CSS length. A width
	# is echoed back into a style attribute, so nothing else is allowed to survive the round trip.
	saved = frappe.parse_json(d.columns) if d.columns else []
	clean = {
		k: v for k, v in widths.items()
		if k in saved and isinstance(v, str) and _WIDTH.match(v.strip())
	}
	frappe.db.set_value("CRM Smart View", view, "column_widths", frappe.as_json(clean),
	                    update_modified=False)
	return {"saved": True, "column_widths": clean}


@frappe.whitelist()
def delete_view(name):
	"""Delete a Smart View — owner-scoped. A standard view (or another user's) is operator-only."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw(_("Not permitted."), frappe.PermissionError)
	doc = frappe.get_doc("CRM Smart View", name)
	if (doc.is_standard or (doc.owner_user and doc.owner_user != user)) and not _is_operator():
		frappe.throw(_("You can only delete your own views."), frappe.PermissionError)
	frappe.delete_doc("CRM Smart View", name, ignore_permissions=True)  # authz-ok: tier-a — smart-view scaffolding, operator-run
	return {"deleted": name}
