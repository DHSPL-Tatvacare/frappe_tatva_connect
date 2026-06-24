"""Smart Views composer — the single READ-ONLY query path for the grain surface.

A Smart View is a saved tabbed list over leads (one row per patient) or activities (one
row per CRM Task of an activity type). Its rows come from ONE `frappe.qb` query that:

  1. drives off CRM Lead (lead view) or CRM Task WHERE custom_task_type = activity_type,
  2. LEFT JOINs only the CRM Lead child tables actually referenced by columns/predicate
     (single-row on parent=name, or a latest_by:<field> subquery); CRM Task activity views
     read the 9 promoted columns directly + display-only JSON_EXTRACT(custom_activity_payload),
  3. ANDs the permission query conditions — ALWAYS, fail-closed, on list AND count,
  4. translates the saved predicate JSON tree into nested qb WHERE (catalog fields only),
  5. applies ad-hoc filters/search/sort (catalog-bounded) and paginates,
  6. returns {columns, rows, total} — total being the tab's PQC-scoped count.

The field catalog (the extended `CRM Lead API Field` master) is the allowlist: no fieldname
that is not a catalog row for the resolved scope can ever reach the SQL. No raw string SQL is
built here — the only raw fragment is the framework's own PQC string, wrapped in a PseudoColumn.
"""
import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.query_builder.functions import Count
from frappe.utils import cint
from pypika.analytics import RowNumber
from pypika.terms import Function, PseudoColumn

from tatva_connect.access import entitlement

LEAD_DOCTYPE = "CRM Lead"
TASK_DOCTYPE = "CRM Task"
PAGE_MAX = 200
PAGE_DEFAULT = 50

# Operators a predicate/filter condition may use -> a qb criterion builder.
_OPS = {
	"=": lambda f, v: f == v,
	"!=": lambda f, v: f != v,
	">": lambda f, v: f > v,
	">=": lambda f, v: f >= v,
	"<": lambda f, v: f < v,
	"<=": lambda f, v: f <= v,
	"like": lambda f, v: f.like("%{0}%".format(v)),
	"not like": lambda f, v: f.not_like("%{0}%".format(v)),
	"in": lambda f, v: f.isin(v if isinstance(v, (list, tuple)) else [v]),
	"not in": lambda f, v: f.notin(v if isinstance(v, (list, tuple)) else [v]),
	"is set": lambda f, v: f.isnotnull(),
	"is not set": lambda f, v: f.isnull(),
}


# ---------------------------------------------------------------------------
# Catalog read — the grain-entitled allowlist for a (base_object, activity_type) scope.
# Reuses the partner-API catalog table (CRM Lead API Field), reading the Smart Views
# columns the partner path ignores. On THIS (internal) path the catalog is grain-filtered
# and role-restricted by the one entitlement brain (tatva_connect.access.entitlement); the
# partner path never does either. applies_to scopes by base object/type.
# ---------------------------------------------------------------------------

def _scope_label(base_object, activity_type):
	return "activity:{0}".format(activity_type) if base_object == "Activity" else "lead"


def _all_catalog_rows():
	"""Every catalog row, keyed by field_key. Request-cached — one read per request, shared
	across every scope/grain resolution (was an uncached get_all per call)."""
	def build():
		rows = frappe.get_all(
			"CRM Lead API Field",
			filters={"sql_source": ["in", ["parent", "child", "task", "payload"]]},
			fields=[
				"field_key", "label", "fieldname", "sql_source", "child_pick",
				"filterable", "sortable", "surface", "applies_to", "target_doctype",
				"grain_vertical", "grain_group", "grain_program",
			],
			order_by="field_key asc",
		)
		return {r.field_key: r for r in rows}

	return entitlement.request_cache("tatva_connect:smartview_catalog", "all", build)


def _catalog_fields(base_object, activity_type, grains, roles):
	"""Catalog rows usable by a view of this base object/type, keyed by field_key, after grain
	entitlement: in scope (applies_to blank or matching), visible in `grains`, minus the fields
	restricted for `roles`, plus the universal floor. The composer never touches a fieldname
	outside this dict — so a saved view can never project a field outside its grain."""
	scope = _scope_label(base_object, activity_type)
	scoped = {}
	for key, r in _all_catalog_rows().items():
		applies = (r.applies_to or "").strip()
		if applies and applies != scope:
			continue
		scoped[key] = r
	return entitlement.resolve_fields(scoped, grains, roles)


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
		df = _col_docfield(r)
		out.append({
			"field_key": r.field_key,
			"label": r.label or r.fieldname,
			"fieldname": r.fieldname,
			"sql_source": r.sql_source,
			"filterable": bool(r.filterable),
			"sortable": bool(r.sortable),
			"surface": r.surface or "worklist",
			# fieldtype + options let the native Filter/ColumnSettings controls (operator menu,
			# value widget, Link target) work off the catalog exactly as they do off doctype meta.
			"fieldtype": df.fieldtype if df else "Data",
			"options": (df.options or "") if df else "",
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
		"activity_type": d.activity_type,
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
		predicate = None
	try:
		columns = frappe.parse_json(d.columns) if d.columns else []
	except Exception:
		columns = []
	return {
		"name": d.name,
		"label": d.label,
		"base_object": d.base_object,
		"activity_type": d.activity_type,
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
		visible = False
	return {"visible": visible}


# ---------------------------------------------------------------------------
# Query assembly.
# ---------------------------------------------------------------------------

def _driving(base_object, activity_type):
	"""(driving DocType name, driving qb table)."""
	return (LEAD_DOCTYPE, DocType(LEAD_DOCTYPE)) if base_object == "Lead" else (TASK_DOCTYPE, DocType(TASK_DOCTYPE))


def _pqc_criterion(driving_name, driving_table):
	"""The framework's permission scope for the driving doctype, as a qb criterion to AND in
	(on rows AND count). Fail-closed: ALWAYS applied when a PQC exists.

	The PQC is the framework's own opaque string (Task -> tasks.permissions; Lead -> crm PQC +
	match conditions), and it references the driving doctype's columns UNQUALIFIED (e.g. bare
	`name`, `lead_owner`). The moment our query LEFT JOINs a child table (every Activity view, and
	any view projecting a child field) those bare columns collide with the child's `name`/`owner`
	-> MySQL 1052 "ambiguous". So we never AND the raw string into the joined query. Instead we
	scope the driving PK through a SINGLE-TABLE subquery where the bare columns resolve cleanly:
	    driving.name IN (SELECT name FROM `tabX` WHERE <pqc>)
	Semantically identical — the PQC only ever constrains the driving doctype's own rows."""
	from frappe.model.db_query import DatabaseQuery

	cond = (DatabaseQuery(driving_name).get_permission_query_conditions() or "").strip()
	if not cond:
		return None
	# Fresh DocType -> renders as `tab<Doctype>` (NOT aliased), so a PQC that prefixes
	# `tabX`.col still binds, and a bare col binds to the subquery's only table.
	src = DocType(driving_name)
	sub = frappe.qb.from_(src).select(src.name).where(PseudoColumn("({0})".format(cond)))  # sqli-ok: framework PQC string from get_permission_query_conditions() — not user input
	return driving_table.name.isin(sub)


def _column_field_keys(view, cat):
	"""The catalog field_keys this view projects, defaulting to every worklist field
	if the saved columns list is empty/invalid. Catalog-bounded."""
	try:
		keys = frappe.parse_json(view.columns) if view.columns else []
	except Exception:
		keys = []
	keys = [k for k in (keys or []) if k in cat]
	if not keys:
		keys = [k for k, r in cat.items() if (r.surface or "worklist") == "worklist"]
	return keys


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
	"""LEFT JOIN every CRM Lead child table referenced by `needed_keys`, once per (doctype, pick).
	Returns (query-mutator, {field_key: pypika Field}). single -> join on parent=name +
	parenttype; latest_by:<f> -> join a subquery picking the newest row per parent. The driving
	table's own (parent/task) fields resolve straight off driving_table; payload fields resolve to
	a JSON_EXTRACT off the task's custom_activity_payload (no join, display-only)."""
	field_terms = {}
	join_specs = {}  # alias -> (aliased child table, pick)
	for key in needed_keys:
		r = cat.get(key)
		if not r:
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
				Function("JSON_EXTRACT", driving_table.custom_activity_payload, "$.{0}".format(r.fieldname)),
			)
			continue
		# child (CRM Lead child table) -> needs a join
		child_dt = (r.target_doctype or "").strip()
		if not child_dt:
			continue
		pick = (r.child_pick or "single").strip()
		alias = "{0}__{1}".format(child_dt, pick).replace(" ", "_").replace(":", "_")
		child_tbl = join_specs.get(alias, (None,))[0]
		if child_tbl is None:
			child_tbl = DocType(child_dt).as_(alias)
			join_specs[alias] = (child_tbl, pick, child_dt)
		# A real Field off the aliased child table -> .as_(field_key) aliases correctly,
		# so the row dict is keyed by field_key (never the bare fieldname).
		field_terms[key] = child_tbl[r.fieldname]

	# the physical table backing the driving doctype (qb aliases tables as `tab<DocType>`).
	driving_tbl = "tab{0}".format(driving_name)

	def apply(query):
		for alias, (child_tbl, pick, child_dt) in join_specs.items():
			if pick.startswith("latest_by:"):
				order_field = pick.split(":", 1)[1]
				inner = DocType(child_dt)
				# ONE row per parent — the newest by `order_field` (name as a deterministic
				# tiebreaker). ROW_NUMBER() OVER (PARTITION BY parent ORDER BY …) then keep rn=1;
				# a bare ORDER-BY subquery would join EVERY child row and duplicate the parent.
				rn = (
					RowNumber()
					.over(inner.parent)
					.orderby(inner[order_field], order=frappe.qb.desc)
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
				).as_(alias)
				query = query.left_join(sub).on(
					PseudoColumn("`{0}`.`parent` = `{1}`.`name`".format(alias, driving_tbl))  # sqli-ok: join on constant/validated identifiers (alias + driving table/name), no user value
				)
			else:
				query = query.left_join(child_tbl).on(
					PseudoColumn(  # sqli-ok: join on constant/validated identifiers (alias + driving table/name), no user value
						"`{0}`.`parent` = `{1}`.`name` AND `{0}`.`parenttype` = '{2}'".format(
							alias, driving_tbl, driving_name
						)
					)
				)
		return query

	return apply, field_terms


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
		return None
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
		field_terms[k].like("%{0}%".format(search))
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
	"""The live DocField backing a catalog row, read from doctype meta (never guessed).
	target_doctype is the doctype the field lives on for every sql_source. None when it can't be
	resolved (payload rows resolve to None -> 'Data', since the schema fieldname is not a real
	CRM Task docfield)."""
	dt = (r.target_doctype or "").strip()
	if not dt:
		return None
	try:
		return frappe.get_meta(dt).get_field(r.fieldname)
	except Exception:
		return None


def _col_fieldtype(r):
	"""The DocField fieldtype for a catalog column — drives the frontend's column width +
	cell formatting (Date/Datetime/Currency...). Unknown -> 'Data' (inert)."""
	df = _col_docfield(r)
	return df.fieldtype if df else "Data"


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
	driving_name, driving_table = _driving(base_object, activity_type)

	col_keys = _column_field_keys(v, cat)
	# Interactive column override wins over the saved set, but stays catalog-bounded: an
	# unknown key is dropped; an empty/invalid request falls back to the saved columns.
	if columns is not None:
		if isinstance(columns, str):
			try:
				columns = frappe.parse_json(columns)
			except Exception:
				columns = None
		if isinstance(columns, (list, tuple)):
			req = [k for k in columns if k in cat]
			if req:
				col_keys = req
	try:
		predicate = frappe.parse_json(v.predicate) if v.predicate else None
	except Exception:
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
	pqc = _pqc_criterion(driving_name, driving_table)
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
		{"key": k, "label": cat[k].label or cat[k].fieldname, "fieldtype": _col_fieldtype(cat[k])}
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
	columns = _validate_columns(view.get("columns"), cat)
	predicate = view.get("predicate")
	if isinstance(predicate, str):
		predicate = frappe.parse_json(predicate) if predicate else None
	if predicate:
		_validate_predicate(predicate, cat)

	operator = _is_operator()
	name = view.get("name")
	if name:
		doc = frappe.get_doc("CRM Smart View", name)
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
	doc.save(ignore_permissions=True)
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
	frappe.delete_doc("CRM Smart View", name, ignore_permissions=True)
	return {"deleted": name}
