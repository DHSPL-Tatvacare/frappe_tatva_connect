"""Smart Views SQL (catalog -> query -> api): join, compare, search and hydrate over catalog keys already proved visible."""
import frappe
from frappe import _
from frappe.database.operator_map import OPERATOR_MAP
from frappe.database.query import Engine
from frappe.query_builder import DocType
from frappe.query_builder.functions import IfNull
from frappe.utils import cstr
from pypika.analytics import RowNumber
from pypika.terms import PseudoColumn, ValueWrapper

from tatva_connect.api import list_link_titles
from tatva_connect.lead import multirow
from tatva_connect.smartview.catalog import _link_master
from tatva_connect.taxonomy import labels

# The group joiners the authoring control offers ("All of" / "Any of" / "None of"), read by validator and builder alike.
_GROUP_OPS = ("and", "or", "not")

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
	"is set": lambda f, v: OPERATOR_MAP["is"](f, "set"),
	"is not set": lambda f, v: OPERATOR_MAP["is"](f, "not set"),
	# The control's default date operator and its named-range sibling.
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
	"""A named timespan ("last week", ...) as its two bounds, resolved by frappe."""
	from frappe.utils import get_timespan_date_range

	span = get_timespan_date_range(cstr(value).lower())
	if not span:
		frappe.throw(_("Unknown timespan {0}").format(value))
	return span


def _predicate_keys(node, acc):
	"""Collect every field_key referenced anywhere in the predicate tree."""
	if not isinstance(node, dict):
		return
	if "conditions" in node:
		for c in node.get("conditions") or []:
			_predicate_keys(c, acc)
	elif node.get("field"):
		acc.add(node["field"])


def _filter_keys(filters, acc):
	"""Collect the field_key of every well-formed ad-hoc filter — the twin of `_predicate_keys`."""
	for f in filters or []:
		if isinstance(f, (list, tuple)) and len(f) == 3:
			acc.add(f[0])


def _joins(needed_keys, cat, driving_table, driving_name):
	"""(query-mutator, {key: read Field}, {key: compared Field}) joining one newest row per parent per needed child or answer."""
	field_terms = {}
	compare_terms = {}  # only where a row is compared somewhere other than where it is read (D17)
	join_specs = {}  # alias -> (aliased child table, row_key_field, child doctype, columns to resolve)
	answer_specs = {}  # alias -> the catalog row whose field this join answers
	for key in needed_keys:
		r = cat.get(key)
		if not r:
			continue
		if r.sql_source == "answer":
			# One join per answer field; the alias is positional because a field_key is not a SQL identifier.
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
		row_key = (r.row_key_field or "").strip()  # multi-row child ordered by its row key; blank -> creation
		alias = f"{child_dt}__{row_key or 'creation'}".replace(" ", "_")
		child_tbl = join_specs.get(alias, (None,))[0]
		if child_tbl is None:
			child_tbl = DocType(child_dt).as_(alias)
			join_specs[alias] = (child_tbl, row_key, child_dt, set())
		# Track the columns asked of this join; `parent` is selected regardless and would collide.
		if r.fieldname != "parent":
			join_specs[alias][3].add(r.fieldname)
		# A real Field off the aliased child table, so the row dict is keyed by field_key.
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
		# One newest row per parent (rn=1), the whole latest row rather than per-column; it only drives filter and sort, `_hydrate` fills display.
		for spec_alias, (_child_tbl, row_key, spec_child_dt, _columns) in join_specs.items():
			inner = DocType(spec_child_dt)
			rn = RowNumber().over(inner.parent)
			for field in multirow.order_keys(row_key):
				rn = rn.orderby(inner[field], order=frappe.qb.desc)
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
	"""A `1=0` pypika Criterion (combinable with `&`/`|`/`~`), so a saved view fails closed on an unresolvable field."""
	return ValueWrapper(1) == ValueWrapper(0)


# Frappe's operator for ours where the names differ, so its null rule reads the operator it knows.
_FRAPPE_OP = {"is set": "is", "is not set": "is", "timespan": "between"}


def _criterion(field_term, op, value, doctype):
	builder = _OPS.get(op)
	if not builder:
		frappe.throw(_("Unsupported operator {0}").format(op))
	return builder(_null_safe(field_term, doctype, op, value), value)


def _null_safe(term, doctype, op, value):
	"""A blank cell compared the way the native list compares it: frappe's own `db_query` IFNULL rule and fallback."""
	engine = Engine()
	engine.db_query_compat = True
	if not engine._should_apply_ifnull(doctype, term.name, _FRAPPE_OP.get(op, op), value):
		return term
	return IfNull(term, PseudoColumn(engine._get_ifnull_fallback(doctype, term.name)))  # sqli-ok: frappe's own typed fallback literal, never a user value


def _predicate_where(node, cat, terms):
	"""A predicate node (group `op` + `conditions`, or leaf `field`/`operator`/`value`) as a qb criterion over the compared `terms`, or None."""
	if not isinstance(node, dict):
		return None
	if "conditions" in node:
		parts = [c for c in (_predicate_where(x, cat, terms) for x in node["conditions"]) if c is not None]
		if not parts:
			return None
		# `not` means "none of these hold": NOT(a OR b …), negating the group rather than each leaf.
		joiner = (node.get("op") or "and").lower()
		crit = parts[0]
		for p in parts[1:]:
			crit = (crit | p) if joiner in ("or", "not") else (crit & p)
		return ~crit if joiner == "not" else crit
	key = node.get("field")
	r = cat.get(key)
	if not r or not r.filterable or key not in terms:
		# An unresolvable saved condition narrows to nothing; dropping it would widen the view.
		return _never_matches()
	# A composite master's label means every key carrying it — the same rule `_apply_filters` asks; a key passes untouched.
	op, value = labels.filter_on(_link_master(r), node.get("operator") or "=", node.get("value"))
	return _criterion(terms[key], op, value, r.target_doctype)


def _apply_filters(crit, filters, cat, terms):
	"""AND ad-hoc filters [[field_key, op, value], ...] on the compared terms; one that cannot be applied throws, never silently skipped."""
	for f in filters or []:
		if not (isinstance(f, (list, tuple)) and len(f) == 3):
			continue
		key, op, value = f
		r = cat.get(key)
		if not r or not r.filterable or key not in terms:
			frappe.throw(_("{0} cannot be filtered on here.").format(key))
		# A composite master's LABEL means every key carrying it — the SAME rule `list_engine` asks, so the two engines cannot answer one question two ways.
		op, value = labels.filter_on(_link_master(r), op, value)
		c = _criterion(terms[key], op, value, r.target_doctype)
		crit = c if crit is None else (crit & c)
	return crit


def _apply_search(crit, search, cat, field_terms, keys):
	"""AND an OR of LIKEs over `keys` on the read terms, the text a user sees."""
	search = (search or "").strip()
	if not search:
		return crit
	likes = [field_terms[k].like(f"%{search}%") for k in keys if k in field_terms]
	if not likes:
		return crit
	sc = likes[0]
	for l in likes[1:]:
		sc = sc | l
	return sc if crit is None else (crit & sc)


# `_lead_titles` archived in .archive/smartview-lead-id-pinned-2026-09-17: the pinned ID chip shows the ID, so no page reads lead titles.


def _link_titles(rows, col_keys, cat, titles):
	"""Fill the shared `_link_titles` map ({target}::{key} -> title) for the page's Link columns via `titles_for`; rows keep their keys."""
	wanted = {}
	for key in col_keys:
		target = _link_master(cat[key])
		if not target:
			continue
		for r in rows:
			if r.get(key):
				wanted.setdefault(target, set()).add(r[key])
	for target, values in wanted.items():
		for value, title in list_link_titles.titles_for(target, values).items():
			titles[f"{target}::{value}"] = title


# A value off the driving row costs a join, and a join makes a page cost the table. Two shapes reach it.
_OFF_ROW_SOURCES = ("answer", "child")


def _hydrate_split(col_keys, must_query, cat):
	"""The PROJECTED columns that leave the page query. `must_query` (filtered/sorted/searched) cannot move — those decide which rows the page holds."""
	return {
		k for k in col_keys
		if k not in must_query and cat.get(k) and cat[k].sql_source in _OFF_ROW_SOURCES
	}


def _hydrate(rows, keys, cat, driving_name):
	"""Fill display-only off-row columns with one `parent IN (page)` read per table; every key is set on every row, None when absent."""
	if not rows or not keys:
		return
	# Keyed as text on both sides: a CRM Task `name` can be an int while a child's `parent` is a varchar.
	names = [cstr(r["name"]) for r in rows]
	by_name = {cstr(r["name"]): r for r in rows}
	for key in keys:
		for r in rows:
			r.setdefault(key, None)

	# One read per table: key-value rows are addressed by fieldname, a child's fields are its columns.
	buckets, child_buckets = {}, {}
	for key in keys:
		row = cat[key]
		if row.sql_source == "child":
			child_buckets.setdefault((row.target_doctype, row.row_key_field or ""), {})[row.fieldname] = key
		else:
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

	# Rows arrive newest-first, so each column takes the first non-blank value per parent.
	for (doctype, row_key), fields in child_buckets.items():
		if not doctype:
			continue
		# The section's ordering column only if it really exists, else `multirow.order_keys` falls back to idx, then name.
		row_key = row_key if frappe.db.has_column(doctype, row_key) else ""
		types = {df.fieldname: df.fieldtype for df in frappe.get_meta(doctype).fields}
		for child in frappe.get_all(  # authz-ok: tier-a — the page's rows already passed the composer's PQC
			doctype,
			filters={"parent": ["in", names], "parenttype": driving_name},
			fields=["parent", *fields],
			order_by=multirow.order_by(row_key),
			limit_page_length=0,
		):
			target = by_name.get(cstr(child.get("parent")))
			if target is None:
				continue
			for fieldname, key in fields.items():
				if multirow.is_blank(target.get(key), types.get(fieldname)):
					target[key] = child.get(fieldname)


def _validate_predicate(node, cat):
	"""Throw unless every group joiner, leaf field (filterable catalog row) and operator in the predicate tree is supported."""
	if not isinstance(node, dict):
		return
	if "conditions" in node:
		# An unsupported joiner is refused rather than read as AND.
		if (node.get("op") or "and").lower() not in _GROUP_OPS:
			frappe.throw(_("Unsupported condition group {0}").format(node.get("op")))
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
