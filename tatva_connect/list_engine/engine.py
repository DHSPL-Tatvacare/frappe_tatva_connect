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

FIVE OPERATORS, ONE MECHANISM. `Filter.vue:335-345` offers Equals / Not equals / In / Not in / Is on a
Select, and `ViewControls.vue:878-891` persists the filter BEFORE the request runs — so an operator that
throws is re-sent on every load and the rep cannot clear it. Each of the five names a SET of buckets. One
bucket inlines its own tuples, unchanged; any other set is a UNION, and frappe's filters are flat and
AND-only (§2 — `or_filters` ORs single conditions, which a multi-tuple bucket is not), so the union is
resolved to the rows those buckets claim through the SAME `get_list` reader `=` uses. The declared tuples
are never negated: De Morgan over NULL-bearing columns is the one class of silent disagreement this layer
exists to prevent.

ONE INSTANT. The clock is read once per request and threaded through translation and projection, so a
row cannot be selected against one instant and displayed against another.

Plan + the hardline rules: docs/plans/tasks-ui/2026-07-30-derived-fields-list-engine.md
"""

import frappe

from tatva_connect.list_engine import derived

KANBAN_PAGE_LENGTH = 20

# A calendar is never paged — the component places every event it is given; this is the backstop.
CALENDAR = "calendar"
CALENDAR_ROW_CAP = 1000


def get_data(**kwargs):
	"""The override. Native answers the original request unless a derived field is named in it.

	The one exception is a view type native does not have: a calendar is always ours, on every doctype,
	because there is no native branch to fall back to."""
	doctype = kwargs.get("doctype")
	view = frappe.parse_json(kwargs.get("view") or "{}") or {}
	if view.get("view_type") != CALENDAR and not (doctype and derived.for_doctype(doctype)):
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
		said.update(self._sorted_by())
		# Board, group-by and title fields each arrive top-level OR inside the saved view; both are read.
		said.add(self.raw.get("column_field"))
		said.add(self.raw.get("group_by_field"))
		said.add(self.raw.get("title_field"))
		said.add(self.view.get("column_field"))
		said.add(self.view.get("group_by_field"))
		said.add(self.view.get("title_field"))
		return tuple(f for f in self.declared if f.fieldname in said)

	def _sorted_by(self):
		"""Every field the sort names, not just the first. `SortBy.vue:277-284` builds "a asc, b desc" and
		lets the rep drag the terms into any order, so reading term one alone sent "modified desc,
		due_state asc" down the native path and the derived name reached SQL."""
		terms = str(self.raw.get("order_by") or "").split(",")
		return [term.strip().split(" ")[0] for term in terms if term.strip()]

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
		return bool(names & set(self._sorted_by()))

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
		"""Every filter as native tuples, each derived one replaced by the rows its buckets claim.

		Also returns the derived-free remainder, which is a legal filters dict — so the call that fetches
		the envelope can be narrowed by it instead of counting the whole table, and so the union below is
		read inside the rep's own scope rather than across the table."""
		from crm.api.doc import convert_filter_to_tuple

		by_name = {f.fieldname: f for f in self.named}
		given = self.filters
		plain = frappe._dict({k: v for k, v in given.items() if k not in by_name})
		scope = convert_filter_to_tuple(self.doctype, plain)
		terms = list(scope)
		for key, value in given.items():
			field = by_name.get(key)
			if field:
				terms.extend(self._predicate(field, value, scope))
		return terms, plain

	def _predicate(self, field, value, scope):
		"""One derived filter as real terms, whichever of the five operators the menu sent.

		A single bucket is its own declared tuples — the fast path, and the only one this took before. Any
		other selection is a union of buckets, which frappe's flat AND-only grammar cannot carry, so it is
		resolved to the rows those buckets claim by the same `get_list` reader `=` uses, narrowed by the
		derived-free filters. The tuples themselves are never negated."""
		chosen, unclaimed = self._chosen(field, value)
		if not unclaimed and len(chosen) == 1:
			return derived.predicate(field, chosen[0], self.snap)

		names = []
		for bucket in chosen:
			names.extend(
				row.name
				for row in frappe.get_list(
					self.doctype,
					fields=["name"],
					filters=[*scope, *derived.predicate(field, bucket, self.snap)],
					limit=0,
				)
			)
		# A name is never blank, so an empty selection reads as "no rows", never as no filter at all.
		return [[self.doctype, "name", "not in" if unclaimed else "in", names or [""]]]

	def _chosen(self, field, value):
		"""The buckets a filter selects, and whether it wants the rows NO bucket claims instead.

		`!=` and `not in` name the OTHER buckets rather than negating anything — the declaration partitions,
		which `derived.verify()` proves — and that is also what a real column does, where `!=` drops NULLs.
		An operator outside the five the menu offers is still refused, as is a value no bucket declares."""
		if not isinstance(value, list | tuple):
			return [self._declared(field, value)], False

		operator = str(value[0]).strip().lower()
		wanted = value[1]
		if operator == "is":
			return list(field.options), str(wanted).strip().lower() == "not set"

		listed = list(wanted) if isinstance(wanted, list | tuple) else [wanted]
		named = [self._declared(field, v) for v in listed]
		if operator in ("=", "==", "in"):
			return named, False
		if operator in ("!=", "not in"):
			return [v for v in field.options if v not in named], False
		frappe.throw(
			frappe._("{0} cannot be filtered with {1}").format(frappe._(field.label), value[0]),
			title=frappe._("Unsupported filter"),
		)

	def _declared(self, field, value):
		"""A value the declaration really has. A saved view holding a renamed bucket asks for one it does
		not, and that is refused readably rather than widened away or answered with a traceback."""
		if field.bucket(value) is None:
			frappe.throw(
				frappe._("{0} has no value called {1}").format(frappe._(field.label), value),
				title=frappe._("Unknown filter value"),
			)
		return value

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

		# A derived card title is withheld so native falls back to its default; `_announce` restores the pick.
		if by_name.get(self.raw.get("title_field")):
			kwargs["title_field"] = None

		terms = [t.strip() for t in str(self.raw.get("order_by") or "").split(",") if t.strip()]
		if any(t.split(" ")[0] in by_name for t in terms):
			rewritten = []
			for term in terms:
				parts = term.split(" ")
				field = by_name.get(parts[0])
				if not field:
					rewritten.append(term)
				elif field.order_by:
					rewritten.append(" ".join([field.order_by, *parts[1:]]))
			# No sort proxy means DROPPED, never passed through — the name would reach SQL as a column.
			kwargs["order_by"] = ", ".join(rewritten) or None

		kwargs.update(overrides)
		return kwargs

	# -- answering ---------------------------------------------------------------------------------

	def answer(self):
		"""Native's verbatim answer when nothing derived is named; otherwise its envelope and our rows."""
		if self.surface == CALENDAR:
			return self.project(self._unpaged())
		if not self.named:
			return _native(self.raw)
		result = self._queried() if self.changes_the_query else _native(self.for_native())
		return self.project(result)

	def _unpaged(self, shell=None):
		"""A calendar is not paged — the component buckets events by date itself (CalendarMonthly.vue:99),
		so it is given the whole set the caller's filters name and draws the ones on screen.

		That is the ONLY thing a calendar needs from the server, and it is why there is no window here: no
		range param, no date column this module has to know about, no refetch when the rep switches month.
		Native has no calendar branch, so it answers the shell (fields, columns, rows) as a list and the rows
		are ours — the same division `_queried` already makes. The cap is a backstop, not paging:
		`total_count` is the truth and exceeding it is logged, never silently truncated."""
		from crm.api.doc import parse_list_data

		shell = shell or _native(
			self.for_native(filters=self.plain, default_filters=None, page_length=1, page_length_count=1)
		)
		rows = shell.get("rows") or ["name"]
		data = frappe.get_list(
			self.doctype,
			fields=rows,
			filters=self.terms,
			order_by=self.for_native().get("order_by"),
			limit=CALENDAR_ROW_CAP,
		)
		shell["data"] = parse_list_data(data, self.doctype)
		shell["total_count"] = self._count(self.terms)
		shell["row_count"] = len(shell["data"])
		shell["view_type"] = CALENDAR
		# The shell was fetched with page_length=1 to buy a cheap count, and `ViewControls.vue:456` reads
		# page_length straight back into the params it re-sends — so echoing the shell's 1 asked the NEXT
		# request for one row. The caller's own page length is echoed, exactly as `_fill_list` echoes it.
		page_length = frappe.cint(self.raw.get("page_length") or 20)
		shell["page_length"] = page_length
		shell["page_length_count"] = frappe.cint(self.raw.get("page_length_count") or page_length)
		if shell["row_count"] < shell["total_count"]:
			frappe.log_error(
				title="Calendar truncated",
				message=f"{self.doctype}: {shell['total_count']} rows match, {CALENDAR_ROW_CAP} drawn",
			)
		return shell

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
		# The shell was fetched with page_length=1, so the four paging keys are restored as `_fill_list` does.
		asked = frappe.cint(self.raw.get("page_length") or 20)
		shell["total_count"] = sum(column["all_count"] for column in columns)
		shell["row_count"] = sum(column["count"] for column in columns)
		shell["page_length"] = asked
		shell["page_length_count"] = frappe.cint(self.raw.get("page_length_count") or asked)

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
		"""Announce the field the way native announces a real one — in `fields`, in `rows`, and on the COLUMN.

		`fields` carries the descriptor, but a table renders each cell from the `columns` entry, and that dict
		is the CALLER's: `ColumnSettings.vue:237-244` builds `{label, type, key, options, width, align}` and
		has never heard of `is_derived`, so `column.is_derived` was undefined on every column and a renderer
		had to branch on the fieldname itself. The flag is therefore stamped onto whichever column dict
		carries the key — the caller's, or the one native loaded from the rep's saved view — and it is
		ADDITIVE: a client that ignores it still renders a correct plain cell."""
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
		if isinstance(columns, list):
			if asked and not any(c.get("key") == field.fieldname for c in columns):
				columns.insert(min(self.asked_columns.index(asked), len(columns)), asked)
			# Label and stamp are the DECLARATION's, never the saved view's snapshot — `CRM View Settings`
			# stores the label a rep's column had when they added it, so renaming in `fields.py` would move
			# four menus and leave the fifth surface reading the old name.
			for column in columns:
				if isinstance(column, dict) and column.get("key") == field.fieldname:
					column["label"] = descriptor["label"]
					column["is_derived"] = descriptor["is_derived"]

		# The card title was withheld from the request for the same reason the column was, so it goes back
		# into the answer. `ViewControls.vue:549` reads `data.title_field` into the params it re-sends, so
		# leaving native's fallback there would silently revert the rep's pick on the next load.
		if self.raw.get("title_field") == field.fieldname:
			result["title_field"] = field.fieldname

		# Native shapes this into a dict only for a field it finds in meta, so a derived one comes back as a
		# bare string and the client's `?.label` reads undefined. It is shaped here to the same contract.
		if result.get("group_by_field") == field.fieldname:
			result["group_by_field"] = {
				"label": descriptor["label"],
				"fieldname": field.fieldname,
				"fieldtype": field.fieldtype,
				"options": list(field.options),
				# Carried so a group HEADER reads the stamp, exactly as a column cell and a card field do.
				# Without it the client has to name the field to know it is derived, which is a second brain.
				"is_derived": descriptor["is_derived"],
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
