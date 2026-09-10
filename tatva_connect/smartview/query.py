"""Smart Views — TURNING A VIEW INTO SQL, and filling in the page.

The second of the composer's three concerns (`catalog` -> `query` -> `api`). It takes the catalog as
given: every key reaching this module has already been proved to exist and to be visible to the caller,
so nothing here re-decides what a field is — it only decides how to ask for it.

  JOIN     only the child tables the columns or the predicate actually reference — single-row on
           parent=name, or a `ROW_NUMBER() … = 1` window ordered by the section's row_key_field.
  COMPARE  the saved predicate tree and the ad-hoc filters, catalog-bounded, through one operator set.
  SEARCH   an OR of LIKEs over the keys the catalog nominates.
  HYDRATE  the columns a view only DISPLAYS, read for the page's own rows AFTER it is fetched — the
           page's LIMIT applies after a join, so a displayed-only child column would otherwise walk the
           whole table to return fifty rows.

No raw string SQL is built here: the only raw fragment is the framework's own PQC string, wrapped in a
PseudoColumn by `access.visibility` and ANDed by the caller.
"""
import frappe
from frappe import _
from frappe.query_builder import DocType
from frappe.utils import cstr
from pypika.analytics import RowNumber
from pypika.terms import PseudoColumn

from tatva_connect.api import list_link_titles
from tatva_connect.lead import multirow
from tatva_connect.smartview.catalog import LEAD_DOCTYPE, _link_master
from tatva_connect.taxonomy import labels

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
	join_specs = {}  # alias -> (aliased child table, row_key_field, child doctype, columns to resolve)
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
		row_key = (r.row_key_field or "").strip()  # multi-row child ordered by its row key; blank -> creation
		alias = f"{child_dt}__{row_key or 'creation'}".replace(" ", "_")
		child_tbl = join_specs.get(alias, (None,))[0]
		if child_tbl is None:
			child_tbl = DocType(child_dt).as_(alias)
			join_specs[alias] = (child_tbl, row_key, child_dt, set())
		# The join resolves exactly the columns this view asks of it, because each one needs its own window;
		# `parent` is selected regardless and would collide with a second copy of itself.
		if r.fieldname != "parent":
			join_specs[alias][3].add(r.fieldname)
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
		# Every child join yields ONE row per parent — the newest by `multirow.order_keys`.
		# ROW_NUMBER() OVER (PARTITION BY parent ORDER BY …), keep rn=1; a plain join would multiply the
		# parent for a multi-row child, inflating rows AND the count.
		#
		# This is the LATEST ROW, deliberately, and NOT the per-column reading `multirow.current_for_section`
		# gives every other consumer. A window per selected column measured 1.4x-6x on the child subquery and
		# the cost grows with the column count, which a list over 173k leads cannot pay. The list's DISPLAYED
		# values do not come through here at all — `_hydrate` fills them page-scoped, in Python, under the
		# shared rule — so this join decides only what a view FILTERS and SORTS on. A view filtering on a
		# column whose value sits on an earlier row can therefore miss those rows; a narrower gap than the
		# latency, and the one place in the app where the two readings are knowingly allowed to differ.
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
	# The SAME rule `_apply_filters` asks two functions below, and the reason it exists: a composite
	# master's picker offers LABELS while the column holds `vertical::group::program::label`, so an
	# equality on one label is really membership of the several keys carrying it. Asked here too, or the
	# ad-hoc path and the saved path answer one question two ways — and the saved one answers with
	# nothing at all, silently. A value that is already a key comes back untouched (labels.py:168), which
	# is what makes this safe on every predicate already stored.
	op, value = labels.filter_on(_link_master(r), node.get("operator") or "=", node.get("value"))
	return _criterion(terms[key], op, value)


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
		# A composite master's LABEL means every key carrying it — the SAME rule `list_engine` asks, so the two engines cannot answer one question two ways.
		op, value = labels.filter_on(_link_master(r), op, value)
		c = _criterion(terms[key], op, value)
		crit = c if crit is None else (crit & c)
	return crit


def _apply_search(crit, search, cat, field_terms, keys):
	"""Free-text search over `keys` (OR of LIKEs) — on the READ term, which is the text a user sees and
	therefore the text they are searching. `_search_keys` decides the set; this only compares."""
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


def _lead_titles(names):
	"""`{"CRM Lead::<id>": title}` — the map LeadCell already reads on every other list, deduped.

	ONE read for the whole page. It used to ask `resolve_title` per name, and that is a per-DOCUMENT
	permission check: with the sales hierarchy on, crm's `has_lead_permission` runs its OWN select for
	every name (org_hierarchy.py:74), so a 200-row page cost hundreds of round trips and an export
	thousands. `get_list` asks the same question — the row gate — once, for every name at once."""
	names = {cstr(n) for n in names if n}
	if not names:
		return {}
	# The framework's own two gates, asked once instead of per name: the target opts in, and the read is scoped.
	meta = frappe.get_meta(LEAD_DOCTYPE)
	if not (meta.show_title_field_in_link and meta.title_field):
		return {}
	rows = frappe.get_list(
		LEAD_DOCTYPE,
		filters={"name": ["in", list(names)]},
		fields=["name", meta.title_field],
		limit_page_length=0,
	)
	return {
		f"{LEAD_DOCTYPE}::{r.name}": r.get(meta.title_field)
		for r in rows
		if r.get(meta.title_field)
	}


def _link_titles(rows, col_keys, cat, titles):
	"""Fill the framework's `_link_titles` map ({target}::{key} -> title) for every Link column on the page.

	THE MAP EVERY OTHER LIST ALREADY SHIPS. `api/list_link_titles` attaches it to the native list, Kanban
	and group-by, and `tatva/linkTitle.js` is its one client reader. This surface used to invent a second
	convention instead — a `<key>_label` written beside each value — so one app resolved a composite key's
	title two ways, and the cell that read it could not be the cell every other list uses.

	It also resolves through `titles_for`, which already knows the thing this file did not: a row-gated
	target answers `has_permission(doc=...)` by loading the whole document, so a per-value lookup cost 293
	queries for 20 rows, while a small master is cheaper read from the doc cache one value at a time.

	The ROW KEEPS ITS KEY, untouched — that is what the view filters, sorts and groups by."""
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

	# Grouped so a second section costs a second read and not a second rule: key-value rows are ADDRESSED
	# by fieldname, a child's fields ARE its columns, so the two group by what each read needs.
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

	# The section's CURRENT reading per parent — the same rule the query's windows apply, now read over the
	# page's parents instead of the table. Rows arrive newest-first and every key was seeded to None above,
	# so the first row that HAS a value for a column is the one that fills it and later rows leave it alone.
	# A single-row section trivially has one row and needs no branch.
	for (doctype, row_key), fields in child_buckets.items():
		if not doctype:
			continue
		# The section's own ordering column, asked of the DATABASE — dropped when it names nothing real, which
		# leaves `multirow.order_keys` on idx, then name.
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
