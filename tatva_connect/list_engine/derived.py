"""A DERIVED FIELD is a column a rep can see, filter, sort, group and board on — that is not in the
database and is never written.

There is no grammar here and no expression language, because frappe already ships one and ships BOTH
readers of it. `frappe.utils.data.evaluate_filters(row, filters)` takes the EXACT filter-tuple structure
that `frappe.get_list(filters=...)` takes and evaluates it against a row in memory. So a bucket is
declared once, as ordinary frappe filter tuples, and the same tuples answer both questions:

    display  ->  evaluate_filters(row, bucket)      "what does this row read as?"
    query    ->  get_list(filters=bucket)           "which rows read as that?"

DECLARATION: a fieldname, a label, and an ORDERED list of `value -> filter tuples`. The first bucket
whose tuples match the row wins. That is the whole model.

THE INVARIANT, and the only mechanism that upholds it. Everything above is true only while the two
readers AGREE. They mostly do, and where they do not the failure is silent and specific: a row stored at
exactly `2026-07-30 23:59:59` is not returned by `due_date <= '2026-07-30 23:59:59'` although
`evaluate_filters` says it matches — so the row is displayed in a bucket the filter will never return,
and it falls out of every board column. Measured on frappe 16.22.0.

That class of defect cannot be enumerated by reasoning. It was found here by a fixture that happened to
sit on the boundary, and two further disagreements predicted from the same reasoning turned out not to
exist. So this module holds NO list of safe operators and NO list of unsafe fieldtype pairs — a curated
list is a guess whose completeness is unknowable. It holds `verify()` instead: for a given declaration
it generates a corpus from the columns that declaration actually reads, puts a row on every boundary
those columns have, and asserts the two readers name identical sets for every bucket. A declaration is
admissible if and only if it verifies, and `tests/list_engine` runs `verify()` over the whole registry so
a field is proven the moment it is declared and nobody has to remember a test.

EXCLUSIVITY is the declaration's obligation and `verify()` proves it too. `value_of` takes the FIRST
matching bucket, but SQL has no such ordering, so overlapping buckets make display and query name
different rows.

ONE INSTANT PER REQUEST. A token resolves against the clock, so resolving it separately for the filter
and for the projection would let one response call a row Overdue in its filter and Due Today in its cell.
`snapshot()` is taken once and threaded through both.

WHY NOT frappe's native virtual DocField: `is_virtual` is form-and-API only — `db_query` neither computes
it nor validates it, so a virtual fieldname in `fields=` reaches SQL and MariaDB throws `Unknown column`.
This module is what makes such a field safe, not merely visible.

WHY NOT a stored column: a derived field may depend on the clock. MariaDB forbids `NOW()` in a PERSISTENT
or indexed generated column (error 3102), Salesforce forbids date literals in a roll-up, and ServiceNow's
stored-calculated escape hatch costs a backfill script plus permanent staleness.

Plan + the hardline rules this module is bound by: docs/plans/tasks-ui/2026-07-30-derived-fields-list-engine.md
"""

import itertools

import frappe
from frappe.model import default_fields, std_fields
from frappe.utils import add_days, add_to_date, get_datetime, now, nowdate
from frappe.utils.data import evaluate_filters

# A declaration holds a token, never a timestamp; it is resolved per request in SITE time.
NOW = "__NOW__"
TOMORROW_START = "__TOMORROW_START__"

_TOKENS = {
	NOW: lambda: now(),
	TOMORROW_START: lambda: f"{add_days(nowdate(), 1)} 00:00:00",
}

_DATE_FIELDTYPES = frozenset({"Date", "Datetime"})
_NUMERIC_FIELDTYPES = frozenset({"Int", "Float", "Currency", "Percent", "Check"})

# A corpus is a product across the declared columns; this bounds it, and `verify` reports when it bites.
CORPUS_LIMIT = 400

# The probe rows never outlive their check, and never reach a delete queue or a doc_event.
_SAVEPOINT = "derived_field_verify"

_REGISTRY = {}


class DerivedFieldError(ValueError):
	pass


def _validated():
	"""Doctypes already checked for a shadowing column, per REQUEST-LOCAL site — a module-level set is
	shared by every site a worker serves, so site B would inherit site A's verdict against a different meta."""
	if not hasattr(frappe.local, "_derived_validated"):
		frappe.local._derived_validated = set()
	return frappe.local._derived_validated


class Disagreement:
	"""One way a declaration fails its invariant, named precisely enough to fix without re-deriving it."""

	__slots__ = ("bucket", "detail", "kind")

	def __init__(self, kind, bucket, detail):
		self.kind = kind
		self.bucket = bucket
		self.detail = detail

	def __repr__(self):
		return f"<{self.kind} {self.bucket}: {self.detail}>"


class Bucket:
	"""One value of a derived field and the filter tuples that define it, as `(fieldname, operator, value)`.
	The doctype is added at resolve time and never declared, so a bucket cannot name another table."""

	__slots__ = ("filters", "value")

	def __init__(self, value, filters):
		self.value = value
		self.filters = tuple(tuple(f) for f in filters)

	def __repr__(self):
		return f"<Bucket {self.value}: {list(self.filters)}>"


class DerivedField:
	"""The declaration. `depends_on` is DERIVED from the buckets, never declared, so the columns the engine
	selects can never drift from the columns the buckets actually read."""

	__slots__ = ("buckets", "depends_on", "doctype", "fieldname", "fieldtype", "label", "order_by")

	def __init__(self, doctype, fieldname, label, buckets, order_by=None, fieldtype="Select"):
		self.doctype = doctype
		self.fieldname = fieldname
		self.label = label
		self.buckets = tuple(buckets)
		self.order_by = order_by
		self.fieldtype = fieldtype
		self.depends_on = tuple(sorted({f[0] for b in self.buckets for f in b.filters}))

	@property
	def options(self):
		return tuple(b.value for b in self.buckets)

	def bucket(self, value):
		for b in self.buckets:
			if b.value == value:
				return b
		return None

	def descriptor(self):
		"""The field shaped as the lens menus and `get_data`'s `columns`/`fields` already shape a REAL one.
		`is_derived` is additive: a client may render a pill from it, and ignoring it yields a plain cell."""
		return {
			"fieldname": self.fieldname,
			"label": frappe._(self.label),
			"fieldtype": self.fieldtype,
			"options": "\n".join(self.options),
			"is_derived": 1,
		}

	def __repr__(self):
		return f"<DerivedField {self.doctype}.{self.fieldname} {list(self.options)}>"


def _validate(field):
	"""Shape checks only. Whether the two readers agree is not knowable here — that is `verify()`."""
	if not field.buckets:
		raise DerivedFieldError(f"{field.fieldname}: at least one bucket is required")

	values = [b.value for b in field.buckets]
	if len(set(values)) != len(values):
		raise DerivedFieldError(f"{field.fieldname}: bucket values must be unique, got {values}")

	for bucket in field.buckets:
		if not bucket.filters:
			raise DerivedFieldError(f"{field.fieldname}/{bucket.value}: a bucket needs at least one filter")
		for f in bucket.filters:
			if len(f) != 3:
				raise DerivedFieldError(
					f"{field.fieldname}/{bucket.value}: a filter is (fieldname, operator, value), got {f!r}"
				)

	if field.fieldname in field.depends_on:
		raise DerivedFieldError(f"{field.fieldname}: a bucket may not filter on the derived field itself")


def register(field):
	"""Declare a derived field. Shape is checked here, at import; agreement is checked by `verify()`, which
	the registry-wide test runs for every declared field."""
	_validate(field)
	_REGISTRY.setdefault(field.doctype, {})[field.fieldname] = field
	_validated().clear()
	return field


def registered():
	"""Every declared field, flat. The registry-wide verification test iterates this and nothing else."""
	return tuple(f for by_name in _REGISTRY.values() for f in by_name.values())


def _assert_declarable(doctype):
	"""A derived fieldname that shadows a real column would make the cell and the column disagree in
	silence. Checked once per doctype per process, against the framework's own field list."""
	if doctype in _validated():
		return
	meta = frappe.get_meta(doctype)
	for fieldname in _REGISTRY.get(doctype, {}):
		if meta.get_field(fieldname) or fieldname in default_fields:
			raise DerivedFieldError(f"{doctype}.{fieldname} already exists as a real field")
	_validated().add(doctype)


def for_doctype(doctype):
	"""Every derived field declared for a doctype, in declaration order. Empty for every doctype that
	declares none, which is what keeps this layer out of the answer until something opts in."""
	declared = _REGISTRY.get(doctype)
	if not declared:
		return ()
	_assert_declarable(doctype)
	return tuple(declared.values())


def get(doctype, fieldname):
	"""One derived field, or None. `None` is the answer for every real field, so callers may ask freely."""
	field = _REGISTRY.get(doctype, {}).get(fieldname)
	if field:
		_assert_declarable(doctype)
	return field


def names(doctype):
	return tuple(_REGISTRY.get(doctype, {}).keys())


def snapshot():
	"""The clock, read ONCE. Thread it through everything serving one request so a row cannot be filtered
	against one instant and displayed against another."""
	return {token: read for token, read in ((t, fn()) for t, fn in _TOKENS.items())}


def _substitute(value, snap):
	if isinstance(value, str):
		return snap.get(value, value)
	if isinstance(value, list | tuple):
		return [_substitute(v, snap) for v in value]
	return value


def resolve(field, bucket, snap=None):
	"""A bucket's declared tuples as frappe filters: the doctype prepended, tokens substituted from the
	snapshot. The SAME list is handed to `evaluate_filters` and to `get_list`."""
	snap = snapshot() if snap is None else snap
	return [[field.doctype, f[0], f[1], _substitute(f[2], snap)] for f in bucket.filters]


def value_of(field, row, snap=None):
	"""The row's value: the first bucket whose filters match. `None` when no bucket claims it — a
	declaration need not be exhaustive, and a blank cell is the honest answer when it is not."""
	snap = snapshot() if snap is None else snap
	for bucket in field.buckets:
		if evaluate_filters(row, resolve(field, bucket, snap)):
			return bucket.value
	return None


def predicate(field, value, snap=None):
	"""The frappe filters selecting exactly the rows whose value is `value`. Raises on an unknown value
	rather than returning an empty list, which would silently widen a rep's list to every row."""
	bucket = field.bucket(value)
	if bucket is None:
		raise DerivedFieldError(f"{field.doctype}.{field.fieldname}: no bucket named {value!r}")
	return resolve(field, bucket, snap)


def project(field, rows, snap=None):
	"""Stamp the derived value onto every row in place. O(page) over columns already selected, no query."""
	snap = snapshot() if snap is None else snap
	for row in rows:
		row[field.fieldname] = value_of(field, row, snap)
	return rows


def domain_of(doctype, fieldname):
	"""What values a real column can hold: its fieldtype, and its closed option set when it has one.

	Answers for the standard columns `meta.get_field` does not, and it is what lets the corpus stay
	honest: an operand the declaration mentions is only a boundary if the COLUMN could ever hold it."""
	df = frappe.get_meta(doctype).get_field(fieldname)
	if df:
		options = df.options.split("\n") if df.fieldtype == "Select" and df.options else None
		return df.fieldtype, options
	for std in std_fields:
		if std["fieldname"] == fieldname:
			return std["fieldtype"], None
	return None, None


def fieldtype_of(doctype, fieldname):
	return domain_of(doctype, fieldname)[0]


def _literals(field, source, snap):
	"""Every resolved operand the declaration compares `source` against, flattened out of `in` lists.

	An operand is not necessarily a column value — `("due_date", "is", "set")` carries a mode word, not a
	date. `_probe_values` filters by the column's domain rather than by operator, so a new operator whose
	operand means something else is handled without this module being told about it."""
	out = []
	for bucket in field.buckets:
		for fieldname, _operator, value in bucket.filters:
			if fieldname != source:
				continue
			resolved = _substitute(value, snap)
			out.extend(resolved if isinstance(resolved, list) else [resolved])
	return out


def _as_datetime(value):
	try:
		return get_datetime(value) if value else None
	except (ValueError, TypeError):
		return None


def _probe_values(fieldtype, options, literals):
	"""Values putting a row on every boundary this column has: each operand the column could hold, either
	side of it, the empty cases, and one value the declaration never mentions."""
	values = [None]
	if fieldtype in _DATE_FIELDTYPES:
		for moment in filter(None, (_as_datetime(literal) for literal in literals)):
			values += [moment, add_to_date(moment, seconds=-1), add_to_date(moment, seconds=1)]
		values.append(get_datetime(f"{nowdate()} 23:59:59"))
	elif fieldtype in _NUMERIC_FIELDTYPES:
		for literal in literals:
			if isinstance(literal, int | float):
				values += [literal, literal - 1, literal + 1]
		values.append(0)
	elif options:
		values += ["", *options]
	else:
		values += ["", *[literal for literal in literals if literal is not None], "__unmentioned__"]

	seen, unique = set(), []
	for value in values:
		if str(value) not in seen:
			seen.add(str(value))
			unique.append(value)
	return unique


def _corpus(field, snap):
	"""One row per combination of boundary values across the declared columns, capped and reported."""
	per_column = []
	for source in field.depends_on:
		fieldtype, options = domain_of(field.doctype, source)
		per_column.append(_probe_values(fieldtype, options, _literals(field, source, snap)))
	combinations = [
		dict(zip(field.depends_on, combo, strict=True)) for combo in itertools.product(*per_column)
	]
	return combinations[:CORPUS_LIMIT], len(combinations)


def verify(field, defaults=None, snap=None):
	"""Prove this declaration on real rows and return every way it fails. Empty means admissible.

	Two properties, both of which a curated operator list can only guess at. READER AGREEMENT: for every
	bucket, the rows SQL returns are exactly the rows `evaluate_filters` claims. PARTITION: no row is
	claimed by two buckets, because SQL has no first-match ordering to fall back on.

	The corpus is generated from the declaration itself — every value it compares against, either side of
	each, plus the empty cases — so a new operator or fieldtype is covered without this module knowing it
	exists. The probe rows live inside a SAVEPOINT and are rolled back, never deleted: `delete_doc` enqueues
	background work and fires doc_events, so a throwaway row would depend on a running worker and could
	trigger real automation. A rollback touches neither."""
	snap = snapshot() if snap is None else snap
	combinations, wanted = _corpus(field, snap)
	problems = []
	if wanted > len(combinations):
		problems.append(
			Disagreement("corpus-capped", None, f"{wanted} combinations wanted, {len(combinations)} verified")
		)

	inserted = []
	frappe.db.savepoint(_SAVEPOINT)
	try:
		for values in combinations:
			doc = frappe.get_doc({"doctype": field.doctype, **(defaults or {}), **values})
			try:
				inserted.append(doc.insert(ignore_permissions=True).name)
			except frappe.ValidationError as unrepresentable:
				problems.append(Disagreement("corpus-unrepresentable", None, f"{values}: {unrepresentable}"))

		scope = [field.doctype, "name", "in", inserted]
		fetched = frappe.get_all(field.doctype, fields=["name", *field.depends_on], filters=[scope], limit=0)

		claims = {}
		for bucket in field.buckets:
			terms = resolve(field, bucket, snap)
			in_sql = {
				r.name
				for r in frappe.get_all(field.doctype, fields=["name"], filters=[*terms, scope], limit=0)
			}
			in_python = {r.name for r in fetched if evaluate_filters(r, terms)}
			if in_sql != in_python:
				problems.append(
					Disagreement(
						"reader-disagreement",
						bucket.value,
						f"sql-only={sorted(in_sql - in_python)} python-only={sorted(in_python - in_sql)}",
					)
				)
			for name in in_python:
				claims.setdefault(name, []).append(bucket.value)

		for name, claimed in claims.items():
			if len(claimed) > 1:
				problems.append(
					Disagreement("overlapping-buckets", claimed[0], f"{name} also in {claimed[1:]}")
				)
	finally:
		frappe.db.rollback(save_point=_SAVEPOINT)

	return problems
