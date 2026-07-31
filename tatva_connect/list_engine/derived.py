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

TWO SOURCES, ONE REGISTRY. A declaration arrives either from `fields.py` (code, at import) or from an
ENABLED `CRM Derived Field` row an operator authored in Desk. Both are answered by `for_doctype` / `get` /
`names`, and nothing downstream — the engine, the five menus, the renderers, the wire contract — is ever
told which one it is holding. That seam is why the operator head needed no change to any of them.

The rows are read ONCE and cached, because `for_doctype` is called on every listing request in the app;
`reload()` drops that cache and `declaration_version()` is the string that changes whenever an enabled row
does, which is what a client keys its own no-expiry caches by.

Plan + the hardline rules this module is bound by: docs/plans/tasks-ui/2026-07-30-derived-fields-list-engine.md
The operator head: docs/plans/tasks-ui/2026-07-31-derived-field-head.md
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

# frappe's empty-case words; the corpus already probes those with `None`, so they name no boundary.
_IS_MODES = frozenset({"set", "not set"})

# A corpus is a product across the declared columns; this bounds it, and `verify` reports when it bites.
CORPUS_LIMIT = 400

# The probe rows never outlive their check, and never reach a delete queue or a doc_event.
_SAVEPOINT = "derived_field_verify"

# The doctype an operator authors a declaration in. Absent until it migrates, which `_load` allows for.
ROW_DOCTYPE = "CRM Derived Field"

# The five field menus, each named by the endpoint that serves it. A declaration offers itself to any set.
COLUMN = "column"
FILTER = "filter"
SORT = "sort"
GROUP_BY = "group_by"
QUICK_FILTER = "quick_filter"
SURFACES = (COLUMN, FILTER, SORT, GROUP_BY, QUICK_FILTER)

# What a default column is worth on screen; a rep resizes it and the saved view keeps their width.
DEFAULT_COLUMN_WIDTH = "10rem"

_CACHE_KEY = "derived_field_rows"

# Explicit invalidation is the mechanism; the TTL is only the self-heal for a drop this app never made.
_CACHE_TTL_SEC = 3600

# A version has to be stable when there is nothing to version, or a client rebuilds its caches every load.
EMPTY_VERSION = "0"

_EMPTY = {"rows": [], "version": EMPTY_VERSION}

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
	The doctype is added at resolve time and never declared, so a bucket cannot name another table.

	`theme` is the badge colour the declaration asks for, so a field authored in Desk needs no code to look
	right. It is a hint and nothing reads it to decide anything — absent or unknown, a client draws gray."""

	__slots__ = ("filters", "theme", "value")

	def __init__(self, value, filters, theme=None):
		self.value = value
		self.filters = tuple(tuple(f) for f in filters)
		self.theme = theme or None

	def __repr__(self):
		return f"<Bucket {self.value}: {list(self.filters)}>"


class DerivedField:
	"""The declaration. `depends_on` is DERIVED from the buckets, never declared, so the columns the engine
	selects can never drift from the columns the buckets actually read.

	`surfaces` names the menus that offer it and `default_column` says it is SHOWN rather than merely
	available. A code declaration passes neither, and both then read as they did before the operator head
	existed: every menu, shown only once a rep adds it."""

	__slots__ = (
		"buckets",
		"default_column",
		"depends_on",
		"doctype",
		"fieldname",
		"fieldtype",
		"label",
		"order_by",
		"surfaces",
	)

	def __init__(
		self,
		doctype,
		fieldname,
		label,
		buckets,
		order_by=None,
		fieldtype="Select",
		surfaces=None,
		default_column=0,
	):
		self.doctype = doctype
		self.fieldname = fieldname
		self.label = label
		self.buckets = tuple(buckets)
		self.order_by = order_by
		self.fieldtype = fieldtype
		self.surfaces = tuple(surfaces) if surfaces else SURFACES
		self.default_column = 1 if default_column else 0
		self.depends_on = tuple(sorted({f[0] for b in self.buckets for f in b.filters}))

	@property
	def options(self):
		return tuple(b.value for b in self.buckets)

	@property
	def themes(self):
		return {b.value: b.theme for b in self.buckets if b.theme}

	def bucket(self, value):
		for b in self.buckets:
			if b.value == value:
				return b
		return None

	def descriptor(self):
		"""The field shaped as the lens menus and `get_data`'s `columns`/`fields` already shape a REAL one.
		`is_derived` is additive: a client may render a pill from it, and ignoring it yields a plain cell.

		`themes` is carried the same way and only when the declaration authored one, so a field that names
		no colour describes itself byte for byte as it did before the key existed."""
		described = {
			"fieldname": self.fieldname,
			"label": frappe._(self.label),
			"fieldtype": self.fieldtype,
			"options": "\n".join(self.options),
			"is_derived": 1,
		}
		themes = self.themes
		if themes:
			described["themes"] = themes
		return described

	def column(self):
		"""The field shaped as a listing COLUMN — the keys `ColumnSettings.vue:237-244` builds and
		`default_list_data` declares, so a column the declaration adds is the one a rep would have added."""
		return {
			"label": frappe._(self.label),
			"type": self.fieldtype,
			"key": self.fieldname,
			"options": "\n".join(self.options),
			"width": DEFAULT_COLUMN_WIDTH,
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

	# A surface nothing serves would be authored, saved and silently offered nowhere, so it is refused here.
	unknown = [s for s in field.surfaces if s not in SURFACES]
	if unknown:
		raise DerivedFieldError(f"{field.fieldname}: {unknown} names no menu; the menus are {list(SURFACES)}")

	# The proxy orders rows WITHIN a bucket, so it must be a bare fieldname a direction can be joined onto.
	if field.order_by is not None:
		if not isinstance(field.order_by, str) or not field.order_by.isidentifier():
			raise DerivedFieldError(
				f"{field.fieldname}: order_by is a bare fieldname, no direction, got {field.order_by!r}"
			)
		if field.order_by == field.fieldname:
			raise DerivedFieldError(
				f"{field.fieldname}: order_by names the real column standing in for it, not itself"
			)


def register(field):
	"""Declare a derived field IN CODE. Shape is checked here, at import; agreement is checked by `verify()`,
	which the registry-wide test runs for every declared field."""
	_validate(field)
	_REGISTRY.setdefault(field.doctype, {})[field.fieldname] = field
	_invalidate()
	return field


def _surfaces(declared):
	"""The menus an authored row offers itself to, off one comma- or newline-separated field.

	Blank means ALL, which is what a code declaration is and what every field was before this existed.
	Nothing is dropped here — an unknown name reaches `_validate`, so the operator is told at Save."""
	named = tuple(
		dict.fromkeys(
			s.strip().lower() for s in str(declared or "").replace("\n", ",").split(",") if s.strip()
		)
	)
	return named or SURFACES


def from_row(row):
	"""One authored `CRM Derived Field` row as a declaration.

	The ONE place a stored row becomes a `DerivedField`, so the controller proving a row at Save proves
	exactly what the loader will serve. Shape is checked here; agreement is `verify()`'s, as it is for code."""
	declared = frappe.parse_json(row.get("buckets") or "[]") or []
	field = DerivedField(
		doctype=row.get("dt"),
		fieldname=row.get("fieldname"),
		label=row.get("label") or row.get("fieldname"),
		buckets=[
			Bucket(b.get("value"), b.get("filters") or [], b.get("theme"))
			for b in declared
			if isinstance(b, dict)
		],
		order_by=row.get("order_by") or None,
		surfaces=_surfaces(row.get("surfaces")),
		default_column=frappe.cint(row.get("default_column")),
	)
	_validate(field)
	return field


def code_declared(doctype, fieldname):
	"""Whether a fieldname is declared IN CODE for a doctype — what an authored row may not collide with.

	A row that shadowed a code declaration would be stored, enabled and then silently never served, because
	code wins the merge. The controller asks this at Save and refuses, so that arm never fires."""
	return fieldname in _REGISTRY.get(doctype, {})


def _load():
	"""Every ENABLED row as a plain payload, plus the version they carry. The only query this module makes.

	The doctype is absent on a site that has not migrated yet, and this sits on the busiest read path — so
	the table test lives INSIDE the build and is cached with the answer, never paid per request."""
	if not frappe.db.table_exists(ROW_DOCTYPE):
		return _EMPTY
	rows = frappe.get_all(
		ROW_DOCTYPE,
		fields=[
			"name",
			"dt",
			"fieldname",
			"label",
			"order_by",
			"surfaces",
			"default_column",
			"buckets",
			"modified",
		],
		filters={"enabled": 1},
		order_by="dt asc, creation asc",
		limit=0,
	)
	stamps = sorted(str(row.modified) for row in rows)
	return {
		"rows": [{k: v for k, v in row.items() if k != "modified"} for row in rows],
		# The count is carried too: dropping the newest row leaves the remaining max where it was.
		"version": f"{len(rows)}:{stamps[-1]}" if stamps else EMPTY_VERSION,
	}


def _rows():
	"""The cached payload. Redis holds it across requests, `frappe.local` across one, `reload()` drops both."""
	# No site bound — an import, or a bench command before `init`: there is no cache and no table to read.
	if not frappe.db:
		return _EMPTY
	payload = getattr(frappe.local, "_derived_rows", None)
	if payload is None:
		payload = frappe.cache().get_value(_CACHE_KEY)
		if payload is None:
			payload = _load()
			frappe.cache().set_value(_CACHE_KEY, payload, expires_in_sec=_CACHE_TTL_SEC)
		frappe.local._derived_rows = payload
	return payload


def _authored():
	"""The enabled rows as declarations, built once per REQUEST off the cached payload.

	`DerivedField` objects are never cached: a pickled object graph outlives the code that defines it across
	a deploy, and rebuilding is a handful of dict walks with no query. A row that no longer builds is SKIPPED
	and logged rather than raised — it was proven at Save, so if a column has been dropped under it since,
	that is one dead field, not every list in the app."""
	built = getattr(frappe.local, "_derived_authored", None)
	if built is None:
		built = []
		for row in _rows()["rows"]:
			try:
				built.append(from_row(frappe._dict(row)))
			except (DerivedFieldError, ValueError, TypeError) as refused:
				frappe.log_error(
					title="Derived field skipped", message=f"{ROW_DOCTYPE} {row.get('name')}: {refused}"
				)
		frappe.local._derived_authored = built
	return built


def _declared_for(doctype):
	"""BOTH sources for one doctype, keyed by fieldname. The only place the two are put together.

	Code wins a collision: an operator can edit a row out from under the app but not `fields.py`, so the
	shipped declaration is the safe one to keep. The controller refuses the collision at Save regardless."""
	merged = dict(_REGISTRY.get(doctype) or {})
	for field in _authored():
		if field.doctype == doctype:
			merged.setdefault(field.fieldname, field)
	return merged


def _invalidate():
	"""Drop what this REQUEST built. The shadow verdict goes with it — a changed declaration may newly
	shadow a real column, and a cached "already checked" would serve an unserveable field."""
	frappe.local._derived_rows = None
	frappe.local._derived_authored = None
	_validated().clear()


def reload(doc=None, method=None):
	"""doc_events hook (`CRM Derived Field` on_update/on_trash) — drop the cache so the next read rebuilds.

	The request-local build goes too, so the row that just saved is live in THIS request as well as the next
	one. That is the whole of "live on Save": no deploy, no migrate, no worker restart."""
	frappe.cache().delete_value(_CACHE_KEY)
	_invalidate()


def declaration_version():
	"""A stable string that changes whenever any ENABLED row does.

	The five field menus are cached in the browser with NO expiry, so this is what a client keys them by;
	without it a rep who loaded the page yesterday would never see a field authored today."""
	return _rows()["version"]


def registered():
	"""Every declared field, flat, from both sources. The registry-wide verification test iterates this."""
	return tuple(f for by_name in _REGISTRY.values() for f in by_name.values()) + tuple(_authored())


def _assert_declarable(doctype):
	"""A derived fieldname that shadows a real column would make the cell and the column disagree in
	silence, and a sort proxy naming no column reaches SQL. Neither is knowable without the framework's own
	field list, so both are checked here — once per doctype per request, on the same meta."""
	if doctype in _validated():
		return
	meta = frappe.get_meta(doctype)
	for fieldname, field in _declared_for(doctype).items():
		if meta.get_field(fieldname) or fieldname in default_fields:
			# Fail-loud is deliberate: a shadowed field is unserveable, so the message names the blast radius.
			raise DerivedFieldError(
				f"{doctype}.{fieldname} already exists as a real field, so every {doctype} list is "
				f"unserveable until the declaration or the column is removed"
			)
		if field.order_by and not (field.order_by in default_fields or fieldtype_of(doctype, field.order_by)):
			raise DerivedFieldError(
				f"{doctype}.{fieldname}: order_by names {field.order_by}, which is not a column on {doctype}"
			)
	_validated().add(doctype)


def for_doctype(doctype):
	"""Every derived field declared for a doctype, code first then authored, in declaration order. Empty for
	every doctype that declares none, which is what keeps this layer out of the answer until something opts
	in — and it stays empty for a site with no enabled row, at the cost of one cached read."""
	declared = _declared_for(doctype)
	if not declared:
		return ()
	_assert_declarable(doctype)
	return tuple(declared.values())


def get(doctype, fieldname):
	"""One derived field, or None. `None` is the answer for every real field, so callers may ask freely."""
	field = _declared_for(doctype).get(fieldname)
	if field:
		_assert_declarable(doctype)
	return field


def names(doctype):
	return tuple(_declared_for(doctype).keys())


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


def _probe_values(fieldtype, options, literals, unrepresentable=None):
	"""Values putting a row on every boundary this column has: each operand the column could hold, either
	side of it, the empty cases, and one value the declaration never mentions.

	An operand the column CANNOT hold is a hole, not a non-event — a `Timespan` word on a Datetime is the
	measured case. It is collected into `unrepresentable` so `verify()` reports the boundary it never sat
	on, rather than probing almost nothing and returning clean."""
	values, rejected = [None], []
	if fieldtype in _DATE_FIELDTYPES:
		for literal in literals:
			moment = _as_datetime(literal)
			if moment is None:
				rejected.append(literal)
				continue
			values += [moment, add_to_date(moment, seconds=-1), add_to_date(moment, seconds=1)]
		values.append(get_datetime(f"{nowdate()} 23:59:59"))
	elif fieldtype in _NUMERIC_FIELDTYPES:
		for literal in literals:
			if isinstance(literal, int | float):
				values += [literal, literal - 1, literal + 1]
			else:
				rejected.append(literal)
		values.append(0)
	elif options:
		values += ["", *options]
		rejected += [literal for literal in literals if literal not in options]
	else:
		values += ["", *[literal for literal in literals if literal is not None], "__unmentioned__"]

	if unrepresentable is not None:
		unrepresentable.extend(
			literal
			for literal in rejected
			if literal is not None and str(literal).strip().lower() not in _IS_MODES
		)

	seen, unique = set(), []
	for value in values:
		if str(value) not in seen:
			seen.add(str(value))
			unique.append(value)
	return unique


def _corpus(field, snap, unrepresentable=None):
	"""One row per combination of boundary values across the declared columns, capped and reported.

	`unrepresentable`, when given, collects `(column, operand)` for every operand that column cannot hold —
	a boundary no probe row sits on, which `verify()` reports rather than passing over in silence."""
	per_column = []
	for source in field.depends_on:
		fieldtype, options = domain_of(field.doctype, source)
		rejected = []
		per_column.append(_probe_values(fieldtype, options, _literals(field, source, snap), rejected))
		if unrepresentable is not None:
			unrepresentable.extend((source, literal) for literal in rejected)
	combinations = [
		dict(zip(field.depends_on, combo, strict=True)) for combo in itertools.product(*per_column)
	]
	return combinations[:CORPUS_LIMIT], len(combinations)


def verify(field, defaults=None, snap=None):
	"""Prove this declaration on real rows and return every way it fails. Empty means admissible.

	Three properties, two of which a curated operator list can only guess at. READER AGREEMENT: for every
	bucket, the rows SQL returns are exactly the rows `evaluate_filters` claims. PARTITION: no row is
	claimed by two buckets, because SQL has no first-match ordering to fall back on. And COVERAGE: every
	operand the declaration names became a probe row, or the boundary it names is reported as unprobed —
	a verifier that may pass vacuously is worse than none, because it is believed.

	The corpus is generated from the declaration itself — every value it compares against, either side of
	each, plus the empty cases — so a new operator or fieldtype is covered without this module knowing it
	exists. The probe rows live inside a SAVEPOINT and are rolled back, never deleted: `delete_doc` enqueues
	background work and fires doc_events, so a throwaway row would depend on a running worker and could
	trigger real automation. A rollback touches neither."""
	snap = snapshot() if snap is None else snap
	holes = []
	combinations, wanted = _corpus(field, snap, holes)
	problems = [
		Disagreement(
			"operand-unrepresentable",
			None,
			f"{source} cannot hold {literal!r}, so no probe row sits on the boundary it names",
		)
		for source, literal in holes
	]
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
				# authz-ok: tier-c — verification only, no session user: probe rows are generated from the
				# declaration, live inside `_SAVEPOINT` and are rolled back before this function returns.
				inserted.append(doc.insert(ignore_permissions=True).name)
			except frappe.ValidationError as unrepresentable:
				problems.append(Disagreement("corpus-unrepresentable", None, f"{values}: {unrepresentable}"))

		scope = [field.doctype, "name", "in", inserted]
		fetched = frappe.get_all(field.doctype, fields=["name", *field.depends_on], filters=[scope], limit=0)

		claims = {}
		for bucket in field.buckets:
			terms = resolve(field, bucket, snap)
			# A reader that RAISES is reported like one that disagrees; a verifier that throws hides the rest.
			try:
				in_sql = {
					r.name
					for r in frappe.get_all(field.doctype, fields=["name"], filters=[*terms, scope], limit=0)
				}
				in_python = {r.name for r in fetched if evaluate_filters(r, terms)}
			except Exception as unreadable:
				problems.append(
					Disagreement("reader-error", bucket.value, f"{type(unreadable).__name__}: {unreadable}")
				)
				continue
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
