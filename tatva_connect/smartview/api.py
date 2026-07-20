"""Smart Views composer — the single READ-ONLY query path for the grain surface.

A Smart View is a saved tabbed list over leads (one row per patient) or activities (one
row per CRM Task of an activity type). Its rows come from ONE `frappe.qb` query that:

  1. drives off CRM Lead (lead view) or CRM Task WHERE custom_task_type = activity_type,
  2. LEFT JOINs only the CRM Lead child tables actually referenced by columns/predicate
     (single-row on parent=name, or ordered by the section's row_key_field); CRM Task activity views
     read the 9 promoted columns directly + display-only JSON_EXTRACT(custom_activity_payload),
  3. ANDs the permission query conditions — ALWAYS, fail-closed, on list AND count,
  4. translates the saved predicate JSON tree into nested qb WHERE (catalog fields only),
  5. applies ad-hoc filters/search/sort (catalog-bounded) and paginates,
  6. returns {columns, rows, total} — total being the tab's PQC-scoped count.

The resolved field set is the allowlist: no fieldname outside it can ever reach the SQL. A lead view
resolves it from `CRM Lead API Field` + `CRM Lead Section`; an activity view asks the activity brain
for the type's schema. No raw string SQL is built here — the only raw fragment is the framework's own
PQC string, wrapped in a PseudoColumn.
"""
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
_NO_JOIN_SOURCES = ("parent", "task", "payload")  # sql_source values answered off the driving row, no join

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
}


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
				fields=["name", "target_doctype", "child_table_field", "is_multi_row", "row_key_field",
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
			rows[r.field_key] = r
		return rows

	return entitlement.request_cache("tatva_connect:smartview_catalog", "all", build)


def _activity_catalog(activity_type):
	"""The activity type's fields, ASKED of the brain. Keyed `activity:<fieldname>` — the schema field's
	own name, so re-keying the TYPE moves nothing here and the type itself is reached through the view's
	Link, which cascades.

	A field naming one of the 9 promoted CRM Task columns IS that column: project, filter and sort it.
	A field naming none lives in the JSON payload, reachable only through JSON_EXTRACT — display-only,
	so it may never reach a WHERE or an ORDER BY (see _joins). The brain owns the routing; this owns
	only what Smart Views can physically do with each side of it."""
	if not activity_type:
		return {}
	rows = {}
	for f in activity_brain.get_schema(activity_type):
		column = activity_brain.field_column(f)
		key = f"activity:{f['fieldname']}"
		rows[key] = frappe._dict(
			field_key=key,
			label=f["label"] or f["fieldname"],
			fieldname=column or f["fieldname"],
			sql_source="task" if column else "payload",
			row_key_field="",
			target_doctype=TASK_DOCTYPE if column else None,
			filterable=1 if column else 0,
			sortable=1 if column else 0,
			surface="worklist" if column else "detail",
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
			"label": r.label or r.fieldname,
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
	"""LEFT JOIN every CRM Lead child table referenced by `needed_keys`, once per (doctype, order_field).
	Returns (query-mutator, {field_key: pypika Field}). No order_field -> join on parent=name +
	parenttype ordered by creation; a row_key_field -> a subquery picking the newest row per parent. The driving
	table's own (parent/task) fields resolve straight off driving_table; payload fields resolve to
	a JSON_EXTRACT off the task's custom_activity_payload (no join, display-only)."""
	field_terms = {}
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
			field_terms[key] = DocType(r.target_doctype).as_(alias)[r.value_field]
			continue
		if r.sql_source in ("parent", "task"):
			field_terms[key] = driving_table[r.fieldname]
			continue
		if r.sql_source == "payload":
			# Display-only: JSON_UNQUOTE(JSON_EXTRACT(<task>.custom_activity_payload, '$.<key>')).
			# The catalog marks payload rows filterable=0/sortable=0, so this term is only ever
			# projected — it never reaches a WHERE/ORDER BY (enforced in _predicate_where/_apply_*).
			field_terms[key] = Function(
				"JSON_UNQUOTE",
				Function("JSON_EXTRACT", driving_table.custom_activity_payload, f"$.{r.fieldname}"),
			)
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

	return apply, field_terms


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


def _predicate_where(node, cat, field_terms):
	"""Translate a predicate node -> a qb criterion (or None). A group has `op`
	(and/or) + `conditions`; a leaf has `field`/`operator`/`value`. Only catalog
	fields with `filterable` reach a clause."""
	if not isinstance(node, dict):
		return None
	if "conditions" in node:
		parts = [c for c in (_predicate_where(x, cat, field_terms) for x in node["conditions"]) if c is not None]
		if not parts:
			return None
		joiner = (node.get("op") or "and").lower()
		crit = parts[0]
		for p in parts[1:]:
			crit = (crit | p) if joiner == "or" else (crit & p)
		return crit
	key = node.get("field")
	r = cat.get(key)
	if not r or not r.filterable or key not in field_terms:
		# A SAVED predicate is the view's definition, so a condition that cannot be resolved narrows to
		# nothing rather than disappearing. Dropped, it widened the view instead: a filter on a question
		# no lead currently answers returned every lead, and one naming a field outside the caller's
		# grain returned more rows than the view was written to show. Ad-hoc filters stay tolerant.
		return _never_matches()
	return _criterion(field_terms[key], node.get("operator") or "=", node.get("value"))


def _apply_filters(crit, filters, cat, field_terms):
	"""Ad-hoc filters: [[field_key, op, value], ...], catalog + filterable bounded.
	Tolerant: an unknown field or unsupported operator is skipped, never raised — a
	stale/odd ad-hoc filter narrows nothing rather than 500-ing the whole list."""
	for f in filters or []:
		if not (isinstance(f, (list, tuple)) and len(f) == 3):
			continue
		key, op, value = f
		r = cat.get(key)
		if not r or not r.filterable or key not in field_terms or op not in _OPS:
			continue
		c = _criterion(field_terms[key], op, value)
		crit = c if crit is None else (crit & c)
	return crit


def _apply_search(crit, search, cat, field_terms):
	"""Free-text search across the projected filterable text fields (OR of LIKEs)."""
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

	apply_joins, field_terms = _joins(needed, cat, driving_table, driving_name)

	# WHERE: predicate tree + ad-hoc filters + search, ALL catalog-bounded.
	crit = _predicate_where(predicate, cat, field_terms)
	crit = _apply_filters(crit, filters, cat, field_terms)
	crit = _apply_search(crit, search, cat, field_terms)
	# Activity view: pin the task type (indexed). Lead view: no extra base filter.
	if base_object == "Activity" and activity_type:
		tc = driving_table.custom_task_type == activity_type
		crit = tc if crit is None else (crit & tc)
	pqc = visibility.readable_criterion(driving_name, driving_table)
	if pqc is not None:
		crit = pqc if crit is None else (crit & pqc)

	# ---- count (PQC-scoped) -------------------------------------------------
	count_q = apply_joins(frappe.qb.from_(driving_table).select(Count("*").as_("total")))
	if crit is not None:
		count_q = count_q.where(crit)
	total = cint(count_q.run(as_dict=True)[0].get("total"))

	# ---- rows ---------------------------------------------------------------
	select_terms = [driving_table.name.as_("name")] + [field_terms[k].as_(k) for k in col_keys if k in field_terms]
	rows_q = apply_joins(frappe.qb.from_(driving_table).select(*select_terms))
	if crit is not None:
		rows_q = rows_q.where(crit)

	# sort: [field_key, "asc"|"desc"], sortable + catalog bounded; default modified desc.
	if isinstance(sort, str):
		sort = frappe.parse_json(sort)
	sort_key = sort[0] if (isinstance(sort, (list, tuple)) and sort) else None
	if sort_key and cat.get(sort_key) and cat[sort_key].sortable and sort_key in field_terms:
		direction = frappe.qb.desc if (len(sort) > 1 and str(sort[1]).lower() == "desc") else frappe.qb.asc
		rows_q = rows_q.orderby(field_terms[sort_key], order=direction)
	else:
		rows_q = rows_q.orderby(driving_table.modified, order=frappe.qb.desc)

	page = max(cint(page) or 1, 1)
	size = min(cint(page_size) or PAGE_DEFAULT, PAGE_MAX)
	rows_q = rows_q.limit(size).offset((page - 1) * size)
	rows = rows_q.run(as_dict=True)

	columns = [
		{"key": k, "label": cat[k].label or cat[k].fieldname, "fieldtype": _col_type(cat[k])[0]}
		for k in col_keys if k in field_terms
	]
	return {"columns": columns, "rows": rows, "total": total}


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
