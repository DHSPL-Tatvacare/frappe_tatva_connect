# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A derived field must be indistinguishable from a real one on every listing surface — and invisible
where nothing declares one.

Six properties, in the order they matter:

  * INERT. A doctype that declares nothing gets a response byte-identical to native's. That is the whole
    safety argument for putting this in the busiest read path in the app, so it is asserted by comparing
    the two responses, not by reasoning about the code path.
  * THE COLUMN AND THE FILTER AGREE. The rows the list SHOWS as `Overdue` are exactly the rows a filter
    on `Overdue` RETURNS. This is the layer's one promise, asserted end to end through `get_data`.
  * EVERY BRANCH CARRIES IT. List, group-by and every kanban column, or a card renders blank (I3).
  * THE MENUS OFFER IT. All four lenses, in the same dict shape a real field arrives in.
  * SORT AND COUNT ARE REAL. Sorting resolves to the declared indexed column; the count carries the same
    narrowing as the page, or "20 of 103" contradicts the screen (C7).
  * NOTHING LEAKS. A rep sees only tasks they may see, whichever path served the query.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_list_engine
"""

import copy
import typing

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, add_to_date, now_datetime, nowdate

from tatva_connect.api import list_link_titles, task_lenses
from tatva_connect.list_engine import derived

TASK = "CRM Task"
PROBE = "EngineProbe"
FIELD = "due_state"


def _rows_arg(*names):
	return list(names)


class ListEngineCase(FrappeTestCase):
	"""One fixture set on every boundary the declaration has, scoped by title so counts stay deterministic."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete(TASK, {"title": ["like", f"{PROBE}%"]})
		now = now_datetime()
		self.expected = {
			"overdue_minute": ("Todo", add_to_date(now, minutes=-1), "Overdue"),
			"overdue_month": ("In Progress", add_to_date(now, days=-30), "Overdue"),
			"due_end_of_today": ("Backlog", f"{nowdate()} 23:59:59", "Due Today"),
			"due_tomorrow": ("Todo", add_to_date(now, days=1), "Upcoming"),
			# THE BOUNDARY ITSELF. Upcoming opens at `>= tomorrow 00:00:00`, so a task due at exactly that
			# instant belongs to it — and to nothing else, because Due Today closes at `< tomorrow 00:00:00`.
			# Read the bound as `>` and this record falls out of EVERY bucket and off the list in silence.
			# The mutation harness planted that exact off-by-one and nothing here noticed until this fixture.
			"due_at_the_bound": ("Todo", f"{add_days(nowdate(), 1)} 00:00:00", "Upcoming"),
			"no_due_date": ("Todo", None, "No Due Date"),
			"done_past_due": ("Done", add_to_date(now, days=-3), "History"),
			"canceled_past_due": ("Canceled", add_to_date(now, days=-3), "History"),
		}
		self.names = {}
		for label, (status, due, _value) in self.expected.items():
			doc = frappe.get_doc(
				{"doctype": TASK, "title": f"{PROBE} {label}", "status": status, "due_date": due}
			).insert(ignore_permissions=True)
			self.names[label] = doc.name

	def tearDown(self):
		frappe.db.delete(TASK, {"title": ["like", f"{PROBE}%"]})

	def _get_data(self, **overrides):
		payload = {
			"doctype": TASK,
			"filters": {"title": ["like", f"{PROBE}%"]},
			"order_by": "creation asc",
			"rows": _rows_arg("name", "title", "status", "due_date", FIELD),
			"page_length": 50,
		}
		payload.update(overrides)
		return list_link_titles.get_data(**payload)

	def _shown(self, result):
		return {r["name"]: r.get(FIELD) for r in result["data"]}


class TestNativeIsUntouched(ListEngineCase):
	"""The line: this layer is unreachable unless a derived field is NAMED in the payload.

	Every shape below is answered by native on the caller's original kwargs, and the assertion is a byte
	comparison of the whole response — not a spot check. Crucially these run on `CRM Task`, the doctype
	that DOES declare a derived field, because a doctype with no declarations proves nothing about the one
	that has one. Compared at the engine boundary so the `_link_titles` map the outer override has always
	added is not mistaken for something this layer introduced."""

	SHAPES: typing.ClassVar[dict] = {
		"plain list, real-column filter": {"filters": {"status": "Todo"}, "order_by": "modified desc"},
		"real-column sort": {"filters": {}, "order_by": "due_date asc"},
		"explicit rows": {"filters": {}, "rows": ["name", "title", "status"]},
		"explicit columns": {
			"filters": {},
			"columns": [{"label": "Title", "type": "Data", "key": "title", "width": "16rem"}],
		},
		"group-by a real column": {
			"filters": {},
			"view": {"view_type": "group_by", "group_by_field": "status"},
		},
		"kanban on a real column": {
			"filters": {},
			"view": {"view_type": "kanban"},
			"column_field": "status",
		},
		"default_filters scoping": {"filters": {"status": "Todo"}, "default_filters": {"priority": "Low"}},
		"@me filter": {"filters": {"assigned_to": "@me"}},
	}

	def test_a_payload_that_names_no_derived_field_is_answered_by_native_byte_for_byte(self):
		from crm.api.doc import get_data as native

		from tatva_connect.list_engine import engine

		for label, shape in self.SHAPES.items():
			with self.subTest(label):
				payload = {"doctype": TASK, "order_by": "creation desc", "page_length": 5, **shape}
				ours = engine.get_data(**copy.deepcopy(payload))
				theirs = native(**copy.deepcopy(payload))
				self.assertEqual(frappe.as_json(ours), frappe.as_json(theirs))

	def test_a_doctype_that_declares_nothing_never_builds_a_request(self):
		from crm.api.doc import get_data as native

		from tatva_connect.list_engine import engine

		payload = {"doctype": "CRM Call Log", "filters": {}, "order_by": "creation desc", "page_length": 5}
		self.assertEqual(
			frappe.as_json(engine.get_data(**copy.deepcopy(payload))),
			frappe.as_json(native(**copy.deepcopy(payload))),
		)

	def test_an_unmentioned_derived_field_costs_the_response_nothing(self):
		result = self._get_data(rows=_rows_arg("name", "title"))
		self.assertTrue(all(FIELD not in row for row in result["data"]))


class TestColumnAndFilterAgree(ListEngineCase):
	def test_the_value_shown_is_the_value_the_declaration_gives(self):
		shown = self._shown(self._get_data())
		for label, (_status, _due, value) in self.expected.items():
			with self.subTest(label):
				self.assertEqual(shown[self.names[label]], value)

	def test_filtering_returns_exactly_the_rows_the_list_shows(self):
		shown = self._shown(self._get_data())
		for value in derived.get(TASK, FIELD).options:
			with self.subTest(value):
				filtered = self._get_data(filters={"title": ["like", f"{PROBE}%"], FIELD: value})
				self.assertEqual(
					{r["name"] for r in filtered["data"]},
					{name for name, v in shown.items() if v == value},
				)

	def test_the_count_carries_the_same_narrowing_as_the_page(self):
		filtered = self._get_data(filters={"title": ["like", f"{PROBE}%"], FIELD: "Overdue"})
		self.assertEqual(filtered["total_count"], 2)
		self.assertEqual(filtered["row_count"], len(filtered["data"]))

	def test_a_task_due_earlier_today_is_overdue_not_due_today(self):
		morning = frappe.get_doc(
			{
				"doctype": TASK,
				"title": f"{PROBE} due_this_morning",
				"status": "Todo",
				"due_date": f"{nowdate()} 00:00:01",
			}
		).insert(ignore_permissions=True)
		self.assertEqual(self._shown(self._get_data())[morning.name], "Overdue")

	def test_filtering_by_a_value_no_bucket_declares_is_refused_readably(self):
		# A saved view holding a renamed bucket must not answer HTTP 500 with a traceback on every load.
		with self.assertRaises(frappe.ValidationError) as caught:
			self._get_data(filters={FIELD: "Nonsense"})
		self.assertIn("Nonsense", str(caught.exception))
		self.assertNotIn("DerivedFieldError", str(caught.exception))


class TestEveryBranchCarriesIt(ListEngineCase):
	def test_the_group_by_branch_carries_the_value_and_its_options(self):
		result = self._get_data(view={"view_type": "group_by", "group_by_field": FIELD})
		self.assertTrue(all(FIELD in row for row in result["data"]))
		self.assertEqual(result["group_by_field"]["fieldname"], FIELD)

	def test_every_kanban_column_carries_the_value(self):
		result = self._get_data(
			view={"view_type": "kanban"},
			column_field=FIELD,
		)
		self.assertEqual(
			[c["name"] for c in result["kanban_columns"]], list(derived.get(TASK, FIELD).options)
		)
		seen = {}
		for column in result["data"]:
			for row in column["data"]:
				self.assertEqual(row.get(FIELD), column["column"]["name"])
				seen[row["name"]] = row[FIELD]
		for label, (_status, _due, value) in self.expected.items():
			self.assertEqual(seen.get(self.names[label]), value, label)

	def test_a_board_column_counts_only_its_own_bucket(self):
		result = self._get_data(view={"view_type": "kanban"}, column_field=FIELD)
		counts = {c["name"]: c["all_count"] for c in result["kanban_columns"]}
		self.assertGreaterEqual(counts["Overdue"], 2)
		self.assertGreaterEqual(counts["History"], 2)


class TestTheMenusOfferIt(ListEngineCase):
	def test_all_four_lenses_offer_the_derived_field(self):
		lenses = {
			"filter": task_lenses.get_filterable_fields(TASK),
			"group_by": task_lenses.get_group_by_fields(TASK),
			"sort": task_lenses.sort_options(TASK),
			"columns": task_lenses.get_column_fields(TASK),
		}
		for name, offered in lenses.items():
			with self.subTest(name):
				entry = next((f for f in offered if f.get("fieldname") == FIELD), None)
				self.assertIsNotNone(entry, f"{name} lens does not offer {FIELD}")
				self.assertEqual(entry["fieldtype"], "Select")
				self.assertEqual(entry["options"].split("\n"), list(derived.get(TASK, FIELD).options))

	def test_all_five_menus_describe_the_field_identically(self):
		"""The anti-drift lock. Five endpoints offer this field and they must all report the ONE description
		the declaration gives, so renaming it in `fields.py` moves every menu at once. Quick filters is the
		one that reads doctype meta directly and so was hand-built at first; it is included here for exactly
		that reason. `options` is deliberately not compared — quick filters legitimately pairs them."""
		declared = derived.get(TASK, FIELD).descriptor()
		# The bar is a stored CHOICE, not a lens: it offers what the rep picked, so the pick is made here
		# before the description is compared. `_store` is that test module's own helper — the setup for a
		# chosen quick filter lives in one place, the same way the field itself does.
		from tatva_connect.tests.list_engine.test_quick_filters import _store

		_store(TASK, ["title", FIELD])
		menus = {
			"filter": task_lenses.get_filterable_fields(TASK),
			"group_by": task_lenses.get_group_by_fields(TASK),
			"sort": task_lenses.sort_options(TASK),
			"columns": task_lenses.get_column_fields(TASK),
			"quick_filters": task_lenses.get_quick_filters(TASK, cached=False),
		}
		for menu, offered in menus.items():
			with self.subTest(menu):
				entry = next((f for f in offered if f.get("fieldname") == FIELD), None)
				self.assertIsNotNone(entry, f"{menu} does not offer {FIELD}")
				for key in ("label", "fieldtype"):
					self.assertEqual(entry.get(key), declared[key], f"{menu} disagrees on {key}")

	def test_quick_filters_pairs_the_options_the_way_that_endpoint_does(self):
		from tatva_connect.tests.list_engine.test_quick_filters import _store

		_store(TASK, ["title", FIELD])
		offered = task_lenses.get_quick_filters(TASK, cached=False)
		entry = next(f for f in offered if f.get("fieldname") == FIELD)
		self.assertEqual(entry["options"][0], {"label": "", "value": ""})
		self.assertEqual([o["value"] for o in entry["options"][1:]], list(derived.get(TASK, FIELD).options))

	def test_no_other_doctype_is_offered_it(self):
		for doctype in ("CRM Lead", "CRM Deal", "FCRM Note", "CRM Call Log"):
			with self.subTest(doctype):
				for menu in (
					task_lenses.get_filterable_fields,
					task_lenses.get_group_by_fields,
					task_lenses.sort_options,
					task_lenses.get_quick_filters,
				):
					offered = menu(doctype)
					self.assertFalse(
						any(f.get("fieldname") == FIELD for f in offered),
						f"{menu.__name__} leaked {FIELD} onto {doctype}",
					)

	def test_a_column_the_caller_added_comes_back_in_the_response(self):
		"""The column is taken OUT of the request so native can resolve the rest, so it has to come back in
		the answer at the position the caller put it — otherwise the client applies the rep's new column, the
		server replies without it, and the table silently reverts. That is exactly what happened in the
		browser: `Due State` could be added and would not stick."""
		asked = [
			{"label": "Title", "type": "Data", "key": "title", "width": "16rem"},
			{"label": "Due State", "type": "Select", "key": FIELD, "width": "10rem"},
		]
		result = self._get_data(columns=asked, rows=_rows_arg("name", "title", FIELD))
		keys = [c.get("key") for c in result["columns"]]
		self.assertIn(FIELD, keys, f"the derived column did not survive the round trip: {keys}")
		self.assertEqual(keys.index(FIELD), 1, "the column came back in the wrong position")

	def test_the_response_announces_it_the_way_it_announces_a_real_field(self):
		result = self._get_data()
		entry = next((f for f in result["fields"] if f.get("fieldname") == FIELD), None)
		self.assertIsNotNone(entry)
		self.assertEqual(entry["is_derived"], 1)


class TestSortAndSafety(ListEngineCase):
	def test_sorting_by_the_derived_field_groups_the_page_by_bucket(self):
		"""A bucketed field sorts BY BUCKET, in declaration order. It used to resolve to the proxy column,
		which put Overdue, History and Upcoming in one interleaved list under a Task Status heading."""
		ordered = self._get_data(order_by=f"{FIELD} asc", rows=_rows_arg("name", "due_date", FIELD))
		seen = [r[FIELD] for r in ordered["data"] if r.get(FIELD)]
		declared = list(derived.get(TASK, FIELD).options)
		self.assertEqual(
			[v for i, v in enumerate(seen) if i == 0 or seen[i - 1] != v],
			sorted({v for v in seen}, key=declared.index),
			"buckets are interleaved; the page is not composed in declaration order",
		)

	def test_sorting_descending_walks_the_buckets_backwards(self):
		ordered = self._get_data(order_by=f"{FIELD} desc", rows=_rows_arg("name", "due_date", FIELD))
		seen = [r[FIELD] for r in ordered["data"] if r.get(FIELD)]
		declared = list(derived.get(TASK, FIELD).options)
		runs = [v for i, v in enumerate(seen) if i == 0 or seen[i - 1] != v]
		self.assertEqual(runs, sorted(set(runs), key=declared.index, reverse=True))

	def test_a_rep_sees_only_their_own_rows_through_the_derived_filter(self):
		# A REP, not merely a non-admin: a probe account another suite leaves behind has no read on this list.
		rep = next(
			(
				user.name
				for user in frappe.get_all(
					"User", filters={"enabled": 1}, fields=["name"], order_by="name asc", limit=0
				)
				if user.name not in ("Administrator", "Guest")
				and frappe.has_permission(TASK, "read", user=user.name)
			),
			None,
		)
		if not rep:
			self.skipTest("no non-admin user with read on this list")
		frappe.set_user(rep)
		try:
			mine = self._get_data(filters={"title": ["like", f"{PROBE}%"], FIELD: "Overdue"})
			permitted = {
				r.name
				for r in frappe.get_list(
					TASK, fields=["name"], filters=[[TASK, "title", "like", f"{PROBE}%"]], limit=0
				)
			}
		finally:
			frappe.set_user("Administrator")
		self.assertTrue({r["name"] for r in mine["data"]} <= permitted)


class TestTheRoutingContract(ListEngineCase):
	"""Which path a payload takes is the contract, so it is asserted directly rather than inferred.

	NATIVE      no derived field named -> `crm.api.doc.get_data` on the caller's original kwargs
	PROJECT     named, but only for display -> native runs, we stamp the value on the rows
	QUERY       named in filters / order_by / column_field -> the predicate is ours, the envelope native's
	"""

	MATRIX: typing.ClassVar[dict] = {
		"real-column filter": ({"filters": {"status": "Todo"}}, "NATIVE"),
		"real-column sort": ({"order_by": "due_date asc"}, "NATIVE"),
		"real-column group-by": ({"view": {"view_type": "group_by", "group_by_field": "status"}}, "NATIVE"),
		"real-column kanban": ({"view": {"view_type": "kanban"}, "column_field": "status"}, "NATIVE"),
		"default_filters only": ({"default_filters": {"status": "Todo"}}, "NATIVE"),
		"derived in rows": ({"rows": ["name", FIELD]}, "PROJECT"),
		"derived in columns": ({"columns": [{"key": FIELD, "label": "Due State"}]}, "PROJECT"),
		"derived in kanban_fields": (
			{"view": {"view_type": "kanban"}, "column_field": "status", "kanban_fields": [FIELD]},
			"PROJECT",
		),
		"derived filter": ({"filters": {FIELD: "Overdue"}}, "QUERY"),
		"derived filter beside a real one": (
			{"filters": {"status": "Todo", FIELD: "Overdue"}},
			"QUERY",
		),
		"derived sort": ({"order_by": f"{FIELD} asc"}, "QUERY"),
		"derived kanban column": (
			{"view": {"view_type": "kanban"}, "column_field": FIELD},
			"QUERY",
		),
	}

	def _request(self, shape):
		from tatva_connect.list_engine.engine import ListRequest

		base = {"doctype": TASK, "filters": {}, "order_by": "creation desc", "page_length": 5}
		return ListRequest({**base, **shape})

	def test_every_payload_takes_the_path_it_is_supposed_to(self):
		for label, (shape, expected) in self.MATRIX.items():
			with self.subTest(label):
				request = self._request(shape)
				actual = (
					"NATIVE" if not request.named else ("QUERY" if request.changes_the_query else "PROJECT")
				)
				self.assertEqual(actual, expected)

	def test_a_native_route_never_reaches_our_code(self):
		# The routing decision and the byte-identical response are two views of the same guarantee.
		from crm.api.doc import get_data as native

		from tatva_connect.list_engine import engine

		for label, (shape, expected) in self.MATRIX.items():
			if expected != "NATIVE":
				continue
			with self.subTest(label):
				base = {"doctype": TASK, "filters": {}, "order_by": "creation desc", "page_length": 5}
				payload = {**base, **shape}
				self.assertEqual(
					frappe.as_json(engine.get_data(**copy.deepcopy(payload))),
					frappe.as_json(native(**copy.deepcopy(payload))),
				)


class TestTheShapesTheFrontendActuallySends(ListEngineCase):
	"""Every confirmed production defect in this layer was a request shape the suite never sent.

	These are not invented payloads: `default_filters` rides every `ViewControls` call, `@me` is what a
	saved "my tasks" view stores, a rep sorts the column they just filtered, and `kanban_columns` is how
	Load More and a persisted drag come back. Each of these went red before the fix that follows it."""

	def test_a_derived_filter_still_honours_default_filters(self):
		# The lead-detail Tasks tab scopes with default_filters; dropping it returned the whole site.
		scoped = self._get_data(
			filters={FIELD: "History"},
			default_filters={"title": ["like", f"{PROBE} done_past_due"]},
		)
		self.assertEqual({r["name"] for r in scoped["data"]}, {self.names["done_past_due"]})
		self.assertEqual(scoped["total_count"], 1)

	def test_a_derived_filter_and_a_derived_sort_survive_each_other(self):
		# The caller's own order_by still names the derived field; sending it to SQL raised at the rep.
		result = self._get_data(
			filters={"title": ["like", f"{PROBE}%"], FIELD: "Overdue"},
			order_by=f"{FIELD} asc",
			rows=_rows_arg("name", "due_date", FIELD),
		)
		dates = [r["due_date"] for r in result["data"]]
		self.assertEqual(dates, sorted(dates))
		self.assertEqual(len(result["data"]), 2)

	def test_a_derived_filter_still_resolves_at_me(self):
		# A saved "my tasks" view stores the literal "@me"; native rewrites it before querying and so must we.
		mine = frappe.get_doc(
			{
				"doctype": TASK,
				"title": f"{PROBE} mine",
				"status": "Todo",
				"due_date": add_to_date(now_datetime(), minutes=-5),
				"assigned_to": frappe.session.user,
			}
		).insert(ignore_permissions=True)
		result = self._get_data(
			filters={"title": ["like", f"{PROBE}%"], "assigned_to": "@me", FIELD: "Overdue"}
		)
		self.assertEqual({r["name"] for r in result["data"]}, {mine.name})

	def test_a_board_column_honours_its_own_page_length(self):
		# Load More grows one column's page_length and refetches; ignoring it pages that column forever.
		result = self._get_data(
			view={"view_type": "kanban"},
			column_field=FIELD,
			kanban_columns=frappe.as_json([{"name": "Overdue", "page_length": 1}]),
		)
		overdue = next(c for c in result["kanban_columns"] if c["name"] == "Overdue")
		self.assertEqual(overdue["all_count"], 2)
		self.assertEqual(overdue["count"], 1)

	def test_a_derived_field_used_as_a_kanban_CARD_field_is_stamped(self):
		# The board groups by a real column here; the derived field is only on the card.
		result = self._get_data(
			view={"view_type": "kanban"},
			column_field="status",
			rows=_rows_arg("name", "title", "status", "due_date"),
			kanban_fields=frappe.as_json(["title", FIELD]),
		)
		rows = [row for column in result["data"] for row in column["data"]]
		self.assertTrue(rows, "the status board returned no rows at all")
		self.assertTrue(all(row.get(FIELD) in derived.get(TASK, FIELD).options for row in rows))

	def test_both_paths_prepare_filters_the_same_way(self):
		"""The one rule this layer states twice — native's `@me` rewrite and `default_filters` merge. The
		delegate path gets them from native; the orchestrate path repeats them. This is their lock."""
		shared = {"title": ["like", f"{PROBE}%"], "assigned_to": "@me"}
		scope = {"status": "Todo"}
		delegated = self._get_data(filters=shared, default_filters=scope)
		orchestrated = self._get_data(filters={**shared, FIELD: "Overdue"}, default_filters=scope)
		expected = {
			r["name"]
			for r in delegated["data"]
			if derived.value_of(derived.get(TASK, FIELD), frappe._dict(r)) == "Overdue"
		}
		self.assertEqual({r["name"] for r in orchestrated["data"]}, expected)


class TestGenericity(ListEngineCase):
	"""A second field, other shape, registered only for this test — the engine must need no change."""

	def setUp(self):
		super().setUp()
		self.second = derived.register(
			derived.DerivedField(
				doctype=TASK,
				fieldname="_probe_priority_band",
				label="Priority Band",
				order_by="priority",
				buckets=[
					derived.Bucket("Hot", [("priority", "=", "High")]),
					derived.Bucket("Warm", [("priority", "=", "Medium")]),
					derived.Bucket("Cool", [("priority", "not in", ["High", "Medium"])]),
				],
			)
		)

	def tearDown(self):
		derived._REGISTRY.get(TASK, {}).pop("_probe_priority_band", None)
		derived._validated().discard(TASK)
		super().tearDown()

	def test_a_second_field_flows_through_with_no_engine_change(self):
		name = self.second.fieldname
		result = self._get_data(rows=_rows_arg("name", "priority", FIELD, name))
		self.assertTrue(all(row.get(name) in self.second.options for row in result["data"]))
		filtered = self._get_data(filters={"title": ["like", f"{PROBE}%"], name: "Cool"})
		self.assertEqual(
			{r["name"] for r in filtered["data"]},
			{r["name"] for r in result["data"] if r[name] == "Cool"},
		)

	def test_it_verifies_like_any_other(self):
		self.assertEqual(derived.verify(self.second, defaults={"title": PROBE}), [])
