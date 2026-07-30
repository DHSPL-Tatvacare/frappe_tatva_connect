"""ONE request, ONE translation, ONE projection — the whole of how a derived field reaches a listing page.

A derived field is declared in `fields.py` and resolved by `derived.py`. This module is the only place
that touches a live request, and it does exactly two things frappe cannot do for itself:

    TRANSLATE   a filter / sort / board on a derived field -> real filter tuples on real columns
    PROJECT     the declared value back onto the rows that come out

Everything else — view settings, columns, rows, kanban shape, counts, form scripts, permissions — is
native's, and is never restated here.

THE LINE, and it is structural, not a convention. `crm.api.doc.get_data` runs on the caller's ORIGINAL,
UNTOUCHED kwargs unless a derived field is named in the payload. `ListRequest.named` is computed before
anything is rewritten, and when it is empty this module returns native's answer verbatim. A doctype that
declares nothing never even builds a request. Both are asserted byte-for-byte in `test_list_engine`,
including on `CRM Task` — the doctype that DOES declare one — for plain filters, sorts, group-by, kanban
and `default_filters`. Real columns behave exactly as they did before this layer existed.

WHY A REQUEST OBJECT. Native prepares a request (`@me`, `default_filters`) inside the same function that
queries, so the preparation cannot be called on its own. It has to be mirrored — once. Before this, the
request was reconstructed ad hoc in six places from three different objects (the raw kwargs, a rewritten
copy, and native's own envelope), and every defect this layer shipped was a read from the wrong one: a
sort taken from a key the envelope never had, board state read from the rewritten copy instead of the
caller's, `default_filters` read by nobody at all. `ListRequest` is built once and is the only thing that
ever reads the raw payload. There is no second answer to "what is this request?".

TWO PATHS, chosen by ONE question — does the payload change the QUERY, or only the DISPLAY?

  * A derived field in `filters`, `order_by` or `column_field` changes which rows come back, and native
    cannot carry the predicate: it coerces `filters` to a dict, and a dict holds one term per column
    while a half-open range needs two. So the query is ours; the envelope is still native's.
  * A derived field only in `rows` / `columns` / `kanban_fields` changes nothing about the query. Native
    runs; we stamp. Group-by needs nothing further because `crm/api/doc.py:494` builds its options from
    the page that came back.

ONE INSTANT. The clock is read once per request and threaded through translation and projection, so a
row cannot be selected against one instant and displayed against another.

Plan + the hardline rules: docs/plans/tasks-ui/2026-07-30-derived-fields-list-engine.md
"""

import frappe

from tatva_connect.list_engine import derived

KANBAN_PAGE_LENGTH = 20


def get_data(**kwargs):
	"""The override. Native answers the original request unless a derived field is named in it."""
	doctype = kwargs.get("doctype")
	if not (doctype and derived.for_doctype(doctype)):
		return _native(kwargs)
	return ListRequest(kwargs).answer()


def lens_fields(doctype):
	"""The derived fields a picker offers, shaped exactly as the native lenses shape a real field."""
	return [
		{**f.descriptor(), "name": f.fieldname, "value": f.fieldname} for f in derived.for_doctype(doctype)
	]


def quick_filter_fields(doctype):
	"""The same fields, adapted to the ONE endpoint that shapes options differently.

	`crm.api.doc.get_quick_filters` is not one of the four lenses — it reads `meta.fields` and selects on
	`in_standard_filter`, so a derived field cannot reach it by any declaration — and it hands the client a
	Select's options already split into `{label, value}` pairs with a leading blank. Only that one key is
	restated; everything else comes from `descriptor()`, so a label or fieldtype changed in `fields.py`
	moves all five menus together and cannot drift here."""
	paired = lambda field: [  # noqa: E731
		{"label": "", "value": ""},
		*({"label": value, "value": value} for value in field.options),
	]
	return [{**f.descriptor(), "options": paired(f)} for f in derived.for_doctype(doctype)]


def _native(kwargs):
	from crm.api.doc import get_data as native

	return native(**frappe.get_newargs(native, kwargs))


class ListRequest:
	"""One listing request, resolved once. The only reader of the raw payload in this module."""

	__slots__ = (
		"asked_columns",
		"board",
		"declared",
		"doctype",
		"named",
		"plain",
		"raw",
		"snap",
		"surface",
		"terms",
		"view",
	)

	def __init__(self, kwargs):
		self.raw = kwargs
		self.doctype = kwargs["doctype"]
		self.declared = derived.for_doctype(self.doctype)
		self.view = frappe.parse_json(kwargs.get("view") or "{}") or {}
		self.surface = self.view.get("view_type") or "list"
		self.snap = derived.snapshot()
		self.asked_columns = self._parse("columns")
		self.named = self._named()
		self.board = self._board_field()
		self.terms, self.plain = self._translate()

	# -- what the payload says ---------------------------------------------------------------------

	def _named(self):
		"""Every declared field the payload names, wherever it names one. Empty means native, untouched."""
		said = set(self._parse("rows"))
		said.update(self._parse("kanban_fields"))
		said.update(c.get("key") or c.get("fieldname") for c in self.asked_columns)
		said.update(self.filters)
		said.add(str(self.raw.get("order_by") or "").split(" ")[0])
		# A board's column field and a group-by field each arrive from two places depending on the surface:
		# top-level on the kwargs, or inside the saved `view`. Both are read, so a derived field named in
		# either is recognised — missing one is how `group_by_field` reached the client as a bare string,
		# which `GroupBy.vue:56` assigns straight into its button and prints as "Group By: undefined".
		said.add(self.raw.get("column_field"))
		said.add(self.raw.get("group_by_field"))
		said.add(self.view.get("column_field"))
		said.add(self.view.get("group_by_field"))
		return tuple(f for f in self.declared if f.fieldname in said)

	def _board_field(self):
		"""The derived field a kanban board is grouped by, when it is grouped by one."""
		if self.surface != "kanban":
			return None
		wanted = (
			self.raw.get("column_field") or self.view.get("column_field") or self.view.get("group_by_field")
		)
		return next((f for f in self.named if f.fieldname == wanted), None)

	@property
	def changes_the_query(self):
		"""Named in `filters`, `order_by` or `column_field` decides WHICH rows; anywhere else only how
		they read. That distinction is the only fork in this module."""
		names = {f.fieldname for f in self.named}
		if self.board or (set(self.filters) & names):
			return True
		return str(self.raw.get("order_by") or "").split(" ")[0] in names

	def _parse(self, key, default="[]"):
		return frappe.parse_json(self.raw.get(key) or default) or []

	@property
	def filters(self):
		"""The filters native would run, not the ones the caller typed.

		Native rewrites `@me` / `%@me%` to the session user and merges `default_filters` into `filters`
		(crm/api/doc.py:283-297) before either reaches a query. That preparation lives inside `get_data`
		and cannot be called on its own, so it is mirrored HERE and nowhere else, in native's own order,
		and locked by `test_both_paths_prepare_filters_the_same_way`."""
		given = frappe._dict(frappe.parse_json(self.raw.get("filters") or {}) or {})
		for key in given:
			value = given[key]
			if isinstance(value, list):
				if "@me" in value:
					value[value.index("@me")] = frappe.session.user
				elif "%@me%" in value:
					for i in [i for i, v in enumerate(value) if v == "%@me%"]:
						value[i] = "%" + frappe.session.user + "%"
			elif value == "@me":
				given[key] = frappe.session.user
		if self.raw.get("default_filters"):
			given.update(frappe.parse_json(self.raw["default_filters"]) or {})
		return given

	# -- the ONE translation -----------------------------------------------------------------------

	def _translate(self):
		"""Every filter as native tuples, each derived one replaced by its bucket's own terms.

		Also returns the derived-free remainder, which is a legal filters dict — so the call that fetches
		the envelope can be narrowed by it instead of counting the whole table."""
		from crm.api.doc import convert_filter_to_tuple

		by_name = {f.fieldname: f for f in self.named}
		plain, terms = frappe._dict(), []
		for key, value in self.filters.items():
			field = by_name.get(key)
			if not field:
				plain[key] = value
				continue
			wanted = value
			if isinstance(value, list | tuple):
				operator, wanted = value[0], value[1]
				if str(operator).lower() not in ("=", "=="):
					frappe.throw(
						frappe._("{0} can be filtered by one value at a time").format(frappe._(field.label)),
						title=frappe._("Unsupported filter"),
					)
			try:
				terms.extend(derived.predicate(field, wanted, self.snap))
			except derived.DerivedFieldError:
				frappe.throw(
					frappe._("{0} has no value called {1}").format(frappe._(field.label), wanted),
					title=frappe._("Unknown filter value"),
				)
		return [*convert_filter_to_tuple(self.doctype, plain), *terms], plain

	def for_native(self, **overrides):
		"""The payload with every derived NAME replaced by the real columns behind it.

		Native resolves fields and board columns through `frappe.get_meta`, which has never heard of a
		derived field, so it is handed the columns the declaration reads and never the name itself."""
		kwargs = dict(self.raw)
		by_name = {f.fieldname: f for f in self.named}

		for key in ("rows", "kanban_fields"):
			listed = self._parse(key)
			if listed:
				real = []
				for name in listed:
					real.extend(by_name[name].depends_on if name in by_name else [name])
				kwargs[key] = frappe.as_json(list(dict.fromkeys(real)))

		if self.asked_columns:
			kwargs["columns"] = frappe.as_json([c for c in self.asked_columns if c.get("key") not in by_name])

		if self.board:
			kwargs["column_field"] = None
			kwargs["kanban_columns"] = "[]"

		parts = str(self.raw.get("order_by") or "").split(" ")
		field = by_name.get(parts[0])
		if field and field.order_by:
			kwargs["order_by"] = " ".join([field.order_by, *parts[1:]])

		kwargs.update(overrides)
		return kwargs

	# -- answering ---------------------------------------------------------------------------------

	def answer(self):
		"""Native's verbatim answer when nothing derived is named; otherwise its envelope and our rows."""
		if not self.named:
			return _native(self.raw)
		result = self._queried() if self.changes_the_query else _native(self.for_native())
		return self.project(result)

	def _queried(self):
		"""The envelope from native, the rows from us — native cannot carry a multi-term predicate.

		The envelope call is narrowed by the derived-free filters and asked for one row: native counts
		whatever it is given, so handing it an empty dict bought a full-table COUNT on every request."""
		shell = _native(
			self.for_native(filters=self.plain, default_filters=None, page_length=1, page_length_count=1)
		)
		if self.board:
			self._fill_board(shell)
		else:
			self._fill_list(shell)
		return shell

	def _fill_list(self, shell):
		from crm.api.doc import parse_list_data

		page_length = frappe.cint(self.raw.get("page_length") or 20)
		data = frappe.get_list(
			self.doctype,
			fields=shell.get("rows") or ["name"],
			filters=self.terms,
			order_by=self.for_native().get("order_by"),
			limit=page_length,
		)
		shell["data"] = parse_list_data(data, self.doctype)
		shell["total_count"] = self._count(self.terms)
		shell["row_count"] = len(shell["data"])
		shell["page_length"] = page_length
		shell["page_length_count"] = frappe.cint(self.raw.get("page_length_count") or page_length)

	def _fill_board(self, shell):
		"""A board column is always ONE bucket, so its predicate is that bucket's terms, unchanged.

		The caller's `kanban_columns` carry each column's own state — `page_length` is what Load More
		grows, `order` is a persisted drag, `delete` hides it — exactly as native's own loop honours them."""
		from crm.api.doc import get_records_based_on_order, getCounts, parse_list_data

		rows = shell.get("rows") or ["name"]
		order_by = self.for_native().get("order_by")
		given = {c.get("name"): c for c in self._parse("kanban_columns")}
		columns, data = [], []

		for value in self.board.options:
			column = {**given.get(value, {}), "name": value}
			terms = [*self.terms, *derived.predicate(self.board, value, self.snap)]
			page_length = frappe.cint(column.get("page_length") or KANBAN_PAGE_LENGTH)
			order = column.get("order")

			if column.get("delete"):
				page = []
			elif order:
				page = get_records_based_on_order(self.doctype, rows, list(terms), page_length, order)
				page = sorted(
					page, key=lambda r: order.index(r["name"]) if r["name"] in order else len(order)
				)
			else:
				page = frappe.get_list(
					self.doctype, fields=rows, filters=terms, order_by=order_by, limit=page_length
				)
			page = parse_list_data(page, self.doctype)
			for row in page:
				getCounts(row, self.doctype)

			column["all_count"] = self._count(terms)
			column["count"] = len(page)
			columns.append(column)
			data.append({"column": column, "fields": shell.get("kanban_fields") or ["name"], "data": page})

		shell["kanban_columns"] = columns
		shell["column_field"] = self.board.fieldname
		shell["data"] = data

	def _count(self, terms):
		from crm.api.doc import COUNT_NAME

		return frappe.get_list(self.doctype, filters=terms, fields=[COUNT_NAME])[0].total_count

	# -- the ONE projection ------------------------------------------------------------------------

	def project(self, result):
		"""Stamp the value on every row of every branch, and announce the field the way native announces
		a real one. Rows are topped up with whatever the declaration reads first — one bounded read, never
		one per row, and only when a column is genuinely absent."""
		if not isinstance(result, dict):
			return result
		for field in self.named:
			for rows in _branches(result):
				self._top_up(field, rows)
				derived.project(field, rows, self.snap)
			self._announce(result, field)
		return result

	def _top_up(self, field, rows):
		missing = [r for r in rows if r.get("name") and any(c not in r for c in field.depends_on)]
		if not missing:
			return
		read = {
			r.name: r
			for r in frappe.get_list(
				self.doctype,
				fields=["name", *field.depends_on],
				filters=[[self.doctype, "name", "in", [r["name"] for r in missing]]],
				limit=0,
			)
		}
		for row in missing:
			row.update({c: read.get(row["name"], {}).get(c) for c in field.depends_on})

	def _announce(self, result, field):
		descriptor = field.descriptor()
		listed = result.setdefault("fields", [])
		if isinstance(listed, list) and not any(
			f.get("fieldname") == field.fieldname for f in listed if isinstance(f, dict)
		):
			listed.append(descriptor)

		rows = result.setdefault("rows", [])
		if isinstance(rows, list) and field.fieldname not in rows:
			rows.append(field.fieldname)

		# The column was taken OUT of the request so native could resolve the rest, so it has to go back
		# into the answer — at the position the caller put it. Without this the client applies the column,
		# the server replies without it, and the table silently reverts the rep's change.
		asked = next((c for c in self.asked_columns if c.get("key") == field.fieldname), None)
		columns = result.setdefault("columns", [])
		if asked and isinstance(columns, list) and not any(c.get("key") == field.fieldname for c in columns):
			columns.insert(min(self.asked_columns.index(asked), len(columns)), asked)

		# Native shapes this into a dict only for a field it finds in meta, so a derived one comes back as a
		# bare string and the client's `?.label` reads undefined. It is shaped here to the same contract.
		if result.get("group_by_field") == field.fieldname:
			result["group_by_field"] = {
				"label": descriptor["label"],
				"fieldname": field.fieldname,
				"fieldtype": field.fieldtype,
				"options": list(field.options),
			}


def _branches(result):
	"""Every list of rows a `get_data` response can carry — the list, the group-by, each board column."""
	data = result.get("data")
	if not isinstance(data, list):
		return
	if data and isinstance(data[0], dict) and "column" in data[0] and "data" in data[0]:
		for column in data:
			yield column.get("data") or []
		return
	yield data
