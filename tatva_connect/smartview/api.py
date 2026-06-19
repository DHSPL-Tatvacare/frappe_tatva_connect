"""Smart Views composer — the single READ-ONLY query path for the grain surface.

A Smart View is a saved tabbed list over leads (one row per patient) or activities (one
row per CRM Task of an activity type). Its rows come from ONE `frappe.qb` query that:

  1. drives off CRM Lead (lead view) or CRM Task WHERE custom_task_type = activity_type,
  2. LEFT JOINs only the child tables actually referenced by columns/predicate
     (single-row on parent=name, or a latest_by:<field> subquery),
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
from pypika.terms import PseudoColumn

from tatva_connect.taxonomy.grain import resolve_scoped

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
# Catalog read — the allowlist for a (base_object, activity_type) scope.
# Reuses the partner-API catalog table (CRM Lead API Field), reading the Smart
# Views columns the partner path ignores. resolve_scoped is NOT used to score the
# catalog rows (the catalog is global, not grain-keyed per row); the grain only
# scopes which Smart Views a user sees (P3). applies_to filters by base object/type.
# ---------------------------------------------------------------------------

def _scope_label(base_object, activity_type):
	return "activity:{0}".format(activity_type) if base_object == "Activity" else "lead"


def _catalog_fields(base_object, activity_type=None):
	"""Catalog rows usable by a view of this base object/type, keyed by field_key.
	A row is in scope if it has a sql_source AND its applies_to is blank or matches
	the scope label. The composer never touches a fieldname outside this dict."""
	scope = _scope_label(base_object, activity_type)
	rows = frappe.get_all(
		"CRM Lead API Field",
		filters={"sql_source": ["in", ["parent", "child", "task", "task_child"]]},
		fields=[
			"field_key", "label", "fieldname", "sql_source", "child_doctype",
			"child_pick", "filterable", "sortable", "surface", "applies_to", "target_doctype",
		],
		order_by="field_key asc",
	)
	out = {}
	for r in rows:
		applies = (r.applies_to or "").strip()
		if applies and applies != scope:
			continue
		out[r.field_key] = r
	return out


@frappe.whitelist()
def field_catalog(base_object, activity_type=None):
	"""The allowed fields for the picker/condition builder, grain + type scoped.
	The single source the editor (P2) and the composer agree on."""
	if base_object not in ("Lead", "Activity"):
		frappe.throw(_("Unknown base object {0}").format(base_object))
	cat = _catalog_fields(base_object, activity_type)
	return [
		{
			"field_key": r.field_key,
			"label": r.label or r.fieldname,
			"fieldname": r.fieldname,
			"sql_source": r.sql_source,
			"filterable": bool(r.filterable),
			"sortable": bool(r.sortable),
			"surface": r.surface or "worklist",
		}
		for r in cat.values()
	]


# ---------------------------------------------------------------------------
# Query assembly.
# ---------------------------------------------------------------------------

def _driving(base_object, activity_type):
	"""(driving DocType name, driving qb table)."""
	return (LEAD_DOCTYPE, DocType(LEAD_DOCTYPE)) if base_object == "Lead" else (TASK_DOCTYPE, DocType(TASK_DOCTYPE))


def _pqc(doctype):
	"""The framework's permission query conditions for the driving doctype, as ONE raw
	criterion ANDed into the query (and the count). Fail-closed: ALWAYS applied. The
	string is the framework's own (Task -> tasks.permissions; Lead -> crm PQC + match
	conditions); never user input."""
	from frappe.model.db_query import DatabaseQuery

	cond = DatabaseQuery(doctype).get_permission_query_conditions() or ""
	cond = cond.strip()
	return PseudoColumn("({0})".format(cond)) if cond else None


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
	"""LEFT JOIN every child table referenced by `needed_keys`, once per (doctype, pick).
	Returns (query-mutator, {field_key: pypika Field}). single -> join on parent=name +
	parenttype; latest_by:<f> -> join a subquery picking the newest row per parent. The
	driving table's own (parent/task) fields resolve straight off driving_table."""
	field_terms = {}
	join_specs = {}  # alias -> (aliased child table, pick)
	for key in needed_keys:
		r = cat.get(key)
		if not r:
			continue
		if r.sql_source in ("parent", "task"):
			field_terms[key] = driving_table[r.fieldname]
			continue
		# child / task_child -> needs a join
		child_dt = r.child_doctype
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
				sub = (
					frappe.qb.from_(inner)
					.select(PseudoColumn("*"))
					.orderby(inner[order_field], order=frappe.qb.desc)
				).as_(alias)
				# Newest row per parent wins on the worklist's single-row expectation.
				query = query.left_join(sub).on(
					PseudoColumn("`{0}`.`parent` = `{1}`.`name`".format(alias, driving_tbl))
				)
			else:
				query = query.left_join(child_tbl).on(
					PseudoColumn(
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
	"""Ad-hoc filters: [[field_key, op, value], ...], catalog + filterable bounded."""
	for f in filters or []:
		if not (isinstance(f, (list, tuple)) and len(f) == 3):
			continue
		key, op, value = f
		r = cat.get(key)
		if not r or not r.filterable or key not in field_terms:
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


@frappe.whitelist()
def get_data(view, filters=None, sort=None, search=None, page=1, page_size=50):
	"""THE composer. Returns {columns, rows, total} for a saved CRM Smart View, PQC-scoped.
	Read-only. One qb query for the rows + one for the count; both AND the PQC."""
	v = frappe.get_doc("CRM Smart View", view)
	base_object = v.base_object
	activity_type = v.activity_type

	cat = _catalog_fields(base_object, activity_type)
	driving_name, driving_table = _driving(base_object, activity_type)

	col_keys = _column_field_keys(v, cat)
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
	pqc = _pqc(driving_name)
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
		{"key": k, "label": cat[k].label or cat[k].fieldname, "fieldtype": "Data"}
		for k in col_keys if k in field_terms
	]
	return {"columns": columns, "rows": rows, "total": total}
