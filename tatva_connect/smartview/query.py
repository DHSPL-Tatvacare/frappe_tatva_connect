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
from tatva_connect.lead import field_value, multirow
from tatva_connect.smartview.catalog import TASK, _col_type, _link_master
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
	join_specs = {}  # alias -> (aliased child table, the section row `multirow` reads, child doctype, columns to resolve)
	answer_specs = {}  # alias -> the catalog row whose field this join answers
	for key in needed_keys:
		r = cat.get(key)
		if not r or r.sql_source == field_value.MULTI_VALUE:
			continue  # selections are no column: `_hydrate` reads them, and nothing compares them in SQL
		if r.sql_source == field_value.ANSWER:
			# One join per answer field; the alias is positional because a field_key is not a SQL identifier.
			alias = f"_tc_ans_{len(answer_specs)}"
			answer_specs[alias] = (key, r)
			aliased = DocType(r.target_doctype).as_(alias)
			field_terms[key] = aliased[r.value_field]
			compare_terms[key] = aliased[r.get("compare_field") or r.value_field]
			continue
		if r.sql_source in (field_value.PARENT, TASK):
			field_terms[key] = driving_table[r.fieldname]
			continue
		# child (CRM Lead child table) -> needs a join
		child_dt = (r.target_doctype or "").strip()
		if not child_dt:
			continue
		alias = child_dt.replace(" ", "_")
		child_tbl = join_specs.get(alias, (None,))[0]
		if child_tbl is None:
			child_tbl = DocType(child_dt).as_(alias)
			join_specs[alias] = (child_tbl, r, child_dt, set())
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
		# One row per parent, the row `multirow.ranking` puts first; whole-row on purpose, since a window per column cost 1.4x-6x (tests/lead/test_multirow_current_reading).
		for spec_alias, (_child_tbl, section, spec_child_dt, _columns) in join_specs.items():
			inner = DocType(spec_child_dt)
			rn = RowNumber().over(inner.parent)
			for field, newest_first in multirow.ranking(section):
				rn = rn.orderby(inner[field], order=frappe.qb.desc if newest_first else frappe.qb.asc)
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


def _always_matches():
	"""A `1=1` Criterion: an unresolvable condition inside "None of", so the negated group still fails closed."""
	return ValueWrapper(1) == ValueWrapper(1)


# Frappe's operator for ours where the names differ, so its null rule reads the operator it knows.
_FRAPPE_OP = {"is set": "is", "is not set": "is", "timespan": "between"}
# Inside a "None of" group a leaf is applied negated, so frappe's null rule is asked about the operator in effect.
_NEGATED_OP = {"=": "!=", "!=": "=", "in": "not in", "not in": "in", "like": "not like", "not like": "like",
               ">": "<=", ">=": "<", "<": ">=", "<=": ">"}


def _criterion(field_term, op, value, doctype, negated=False):
	builder = _OPS.get(op)
	if not builder:
		frappe.throw(_("Unsupported operator {0}").format(op))
	in_effect = _NEGATED_OP.get(op, op) if negated else op
	# A negated range has no frappe operator to ask, and "not in this range" includes a blank date.
	blank_matches = negated and op in ("between", "timespan")
	return builder(_null_safe(field_term, doctype, in_effect, value, blank_matches), value)


def _null_safe(term, doctype, op, value, force=False):
	"""A blank cell compared the way the native list compares it: frappe's own `db_query` IFNULL rule and fallback."""
	engine = Engine()
	engine.db_query_compat = True
	if not force and not engine._should_apply_ifnull(doctype, term.name, _FRAPPE_OP.get(op, op), value):
		return term
	return IfNull(term, PseudoColumn(engine._get_ifnull_fallback(doctype, term.name)))  # sqli-ok: frappe's own typed fallback literal, never a user value


def _predicate_where(node, cat, terms, negated=False):
	"""A predicate node (group `op` + `conditions`, or leaf `field`/`operator`/`value`) as a qb criterion over the compared `terms`, or None."""
	if not isinstance(node, dict):
		return None
	if "conditions" in node:
		# `not` means "none of these hold": NOT(a OR b …), negating the group rather than each leaf.
		joiner = (node.get("op") or "and").lower()
		inner = negated != (joiner == "not")
		parts = [c for c in (_predicate_where(x, cat, terms, inner) for x in node["conditions"]) if c is not None]
		if not parts:
			return None
		crit = parts[0]
		for p in parts[1:]:
			crit = (crit | p) if joiner in ("or", "not") else (crit & p)
		return ~crit if joiner == "not" else crit
	key = node.get("field")
	r = cat.get(key)
	if not r or not r.filterable or key not in terms:
		# An unresolvable saved condition narrows to nothing; dropping it would widen the view.
		return _always_matches() if negated else _never_matches()
	# A composite master's label means every key carrying it — the same rule `_apply_filters` asks; a key passes untouched.
	op, value = labels.filter_on(_link_master(r), node.get("operator") or "=", node.get("value"))
	return _criterion(terms[key], op, value, r.target_doctype, negated)


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


def _link_selectors(col_keys, cat, driving_table):
	"""The selector column of every projected driving-row Dynamic Link, so the page can name each row's target as the native list does."""
	out = {}
	for key in col_keys:
		fieldtype, options = _col_type(cat[key])
		if fieldtype == "Dynamic Link" and options and cat[key].sql_source in (field_value.PARENT, TASK):
			out[options] = driving_table[options].as_(options)
	return list(out.values())


def _link_titles(rows, col_keys, cat, titles):
	"""Fill the shared `_link_titles` map ({target}::{key} -> title) for the page's Link and Dynamic Link columns via `titles_for`; rows keep their keys."""
	wanted = {}
	for key in col_keys:
		fieldtype, options = _col_type(cat[key])
		if fieldtype not in ("Link", "Dynamic Link") or not options:
			continue
		for r in rows:
			# A Dynamic Link's target is the row's own selector column, read as `list_link_titles` reads it.
			target = r.get(options) if fieldtype == "Dynamic Link" else options
			if target and r.get(key):
				wanted.setdefault(target, set()).add(r[key])
	for target, values in wanted.items():
		for value, title in list_link_titles.titles_for(target, values).items():
			titles[f"{target}::{value}"] = title


# A value off the driving row costs a join, and a join makes a page cost the table. Two shapes reach it.
_OFF_ROW_SOURCES = (field_value.ANSWER, field_value.CHILD)


def _hydrate_split(col_keys, must_query, cat):
	"""The PROJECTED columns that leave the page query. `must_query` (filtered/sorted/searched) cannot move — those decide which rows the page holds; selections are never in it."""
	return {
		k for k in col_keys
		if cat.get(k) and (cat[k].sql_source == field_value.MULTI_VALUE
		                   or (k not in must_query and cat[k].sql_source in _OFF_ROW_SOURCES))
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

	# One read per table: key-value rows are addressed by fieldname, a child's fields are its columns, selections by section.
	buckets, child_buckets, multi_buckets = {}, {}, {}
	for key in keys:
		row = cat[key]
		if row.sql_source == field_value.MULTI_VALUE:
			multi_buckets.setdefault(row.section, []).append(key)
		elif row.sql_source == field_value.CHILD:
			child_buckets.setdefault(row.target_doctype, (row, {}))[1][row.fieldname] = key
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
			order_by="idx asc",  # the last write wins, so each answer is its newest row, as `keyvalue.newest_first` reads it
			limit_page_length=0,
		):
			target = by_name.get(cstr(answer.get("parent")))
			key = fields.get(answer.get(address))
			if target is not None and key:
				target[key] = answer.get(value_field)

	# Each parent's rows read by `multirow.reading`, the one reading the Data tab and the task form show.
	for doctype, (section, fields) in child_buckets.items():
		if not doctype:
			continue
		by_parent = {}
		for child in frappe.get_all(  # authz-ok: tier-a — the page's rows already passed the composer's PQC
			doctype,
			filters={"parent": ["in", names], "parenttype": driving_name},
			fields=list(dict.fromkeys(["parent", "name", "idx", *multirow.order_keys(section.row_key_field), *fields])),
			order_by="idx asc",
			limit_page_length=0,
		):
			by_parent.setdefault(cstr(child.parent), []).append(child)
		for parent, rows in by_parent.items():
			current = multirow.reading(rows, section) or {}
			for fieldname, key in fields.items():
				by_name[parent][key] = current.get(fieldname)

	for section_key, keys_here in multi_buckets.items():
		section = frappe.get_cached_doc("CRM Lead Section", section_key)
		held = field_value.page_selections(names, driving_name, section, [cat[k] for k in keys_here])
		for key in keys_here:
			df = field_value.docfield(section, cat[key])
			for name, target in by_name.items():
				values = held.get((name, cstr(cat[key].field_key)))
				target[key] = field_value.as_text(df, values) if values else None


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
