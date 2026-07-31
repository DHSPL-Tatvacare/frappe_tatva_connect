# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The four ways a request can NAME a derived field and be answered wrongly — and the one rule that binds
every one of the fixes.

`test_list_engine` proves the layer's promise on the shapes it already served. This file covers the four
audited defects where the request shape itself was mis-read (freeze list §16, items 1, 4, 5, 6):

  * FIVE OPERATORS, NOT ONE. `Filter.vue` offers Equals / Not equals / In / Not in / Is on a Select and
    `ViewControls.vue` persists the filter BEFORE the request runs, so an operator that throws is re-sent
    on every load and there is no chip to remove on a cold one. The rep is locked out of their own list.
  * THE CARD TITLE IS A NAME TOO. A derived `title_field` was read by nobody, went native, and every card
    read "No Title".
  * A SORT HAS MORE THAN ONE TERM. `"modified desc, due_state asc"` was not detected, went native, and the
    derived name reached SQL as a column — `PermissionError`, measured on the bench.
  * A BOARD ANSWERS A PAGE LENGTH. The shell is fetched with `page_length=1`; the board never wrote the
    caller's own back, so the NEXT view asked for one row.

Every class carries a NATIVE-UNTOUCHED case: the same request shape with only real columns must still be
answered by `crm.api.doc.get_data` on the caller's own arguments, byte for byte. Those cases are green
before and after by construction — they are the lock on the one rule, not the proof of the fix.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.list_engine.test_engine_translation
"""

import copy
import typing

import frappe

from tatva_connect.list_engine import derived, engine, fields
from tatva_connect.tests.list_engine.test_list_engine import PROBE, TASK, ListEngineCase, _rows_arg

FIELD = fields.DUE_STATE.fieldname


class TranslationCase(ListEngineCase):
	"""The fixture set of `ListEngineCase`, plus the two things every class here asks."""

	def _request(self, **shape):
		base = {"doctype": TASK, "filters": {}, "order_by": "creation desc", "page_length": 5}
		return engine.ListRequest({**base, **shape})

	def _identical_to_native(self, **shape):
		from crm.api.doc import get_data as native

		payload = {"doctype": TASK, "filters": {}, "order_by": "creation desc", "page_length": 5, **shape}
		self.assertEqual(
			frappe.as_json(engine.get_data(**copy.deepcopy(payload))),
			frappe.as_json(native(**copy.deepcopy(payload))),
		)

	def _rows_for(self, filter_value):
		result = self._get_data(filters={"title": ["like", f"{PROBE}%"], FIELD: filter_value})
		return result, {r["name"] for r in result["data"]}

	def _fixtures_reading(self, *values):
		return {name for name, value in self._shown(self._get_data()).items() if value in values}


class TestEveryOperatorTheMenuOffers(TranslationCase):
	"""Item #1. Four of the five operators threw, and the throwing filter is persisted before the request
	runs — so the rep cannot get back to their own list. Each one is asserted against the rows the list
	SHOWS, which is the layer's one promise, not against a hand-written expected set."""

	REFUSED: typing.ClassVar[list] = [
		"Nonsense",
		["!=", "Nonsense"],
		["in", ["Overdue", "Nonsense"]],
		["not in", ["Nonsense"]],
	]

	REAL_COLUMN: typing.ClassVar[dict] = {
		"not equals": {"filters": {"status": ["!=", "Todo"]}},
		"in": {"filters": {"status": ["in", ["Todo", "Done"]]}},
		"not in": {"filters": {"status": ["not in", ["Done"]]}},
		"is set": {"filters": {"due_date": ["is", "set"]}},
		"is not set": {"filters": {"due_date": ["is", "not set"]}},
	}

	def test_not_equals_returns_exactly_the_complement(self):
		_result, got = self._rows_for(["!=", "Overdue"])
		self.assertEqual(got, self._fixtures_reading("Due Today", "Upcoming", "No Due Date", "History"))

	def test_in_returns_the_union_of_the_named_buckets(self):
		_result, got = self._rows_for(["in", ["Overdue", "History"]])
		self.assertEqual(got, self._fixtures_reading("Overdue", "History"))

	def test_not_in_returns_the_union_of_all_the_others(self):
		_result, got = self._rows_for(["not in", ["History"]])
		self.assertEqual(got, self._fixtures_reading("Overdue", "Due Today", "Upcoming", "No Due Date"))

	def test_is_set_is_every_bucket_and_is_not_set_is_none_of_them(self):
		_result, every = self._rows_for(["is", "set"])
		self.assertEqual(every, self._fixtures_reading(*fields.DUE_STATE.options))
		_result, none = self._rows_for(["is", "not set"])
		self.assertEqual(none, set())

	def test_a_filter_that_selects_no_bucket_returns_no_rows_rather_than_every_row(self):
		# The honest answer to "not in (everything)" is nothing. Emitting no filter at all would widen the
		# rep's list to the whole table, which is the one outcome this layer may not ship.
		result, got = self._rows_for(["not in", list(fields.DUE_STATE.options)])
		self.assertEqual(got, set())
		self.assertEqual(result["total_count"], 0)

	def test_the_count_carries_the_same_narrowing_as_the_page_for_a_union(self):
		result, got = self._rows_for(["!=", "Overdue"])
		self.assertEqual(result["total_count"], len(got))
		self.assertEqual(result["row_count"], len(result["data"]))

	def test_an_undeclared_value_is_refused_readably_by_every_operator(self):
		# A saved view holding a renamed bucket must not answer a traceback, whichever operator stored it.
		for value in self.REFUSED:
			with self.subTest(value):
				with self.assertRaises(frappe.ValidationError) as caught:
					self._get_data(filters={FIELD: value})
				self.assertIn("Nonsense", str(caught.exception))

	def test_an_operator_the_menu_never_offers_is_still_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._get_data(filters={FIELD: ["like", "%Over%"]})
		self.assertIn(fields.DUE_STATE.label, str(caught.exception))

	def test_the_same_operators_on_a_real_column_are_answered_by_native_byte_for_byte(self):
		for label, shape in self.REAL_COLUMN.items():
			with self.subTest(label):
				self._identical_to_native(**shape)


class TestTheCardTitleIsANameToo(TranslationCase):
	"""Item #4. `KanbanSettings.vue:31-43` offers the Title Field picker the unfiltered field list, so a rep
	can pick Task Status as the card title. Nothing read `title_field`, so the request went native."""

	BOARD: typing.ClassVar[dict] = {"view": {"view_type": "kanban"}, "column_field": "status"}

	def test_a_derived_title_field_is_named_and_changes_only_the_display(self):
		request = self._request(**self.BOARD, title_field=FIELD)
		self.assertIn(FIELD, [f.fieldname for f in request.named])
		self.assertFalse(request.changes_the_query, "a card title decides how rows read, never which rows")

	def test_a_derived_title_field_is_withheld_from_native(self):
		# Native resolves the title through `frappe.get_meta`, appends it to `rows` (doc.py:390-391) and
		# frappe drops the unknown column in silence — so the card reads "No Title" and nothing errors.
		self.assertIsNone(self._request(**self.BOARD, title_field=FIELD).for_native()["title_field"])

	def test_every_card_carries_the_title_the_rep_picked(self):
		result = self._get_data(
			**self.BOARD,
			title_field=FIELD,
			rows=_rows_arg("name", "title", "status", "due_date"),
		)
		self.assertEqual(result["title_field"], FIELD, "the rep's pick did not survive the round trip")
		rows = [row for column in result["data"] for row in column["data"]]
		self.assertTrue(rows, "the status board returned no rows at all")
		self.assertTrue(all(row.get(FIELD) in fields.DUE_STATE.options for row in rows))

	def test_a_real_title_field_is_answered_by_native_byte_for_byte(self):
		self._identical_to_native(**self.BOARD, title_field="title")


class TestASortHasMoreThanOneTerm(TranslationCase):
	"""Item #5. `SortBy.vue:277-284` builds "a asc, b desc" and lets the rep drag the terms into any order.
	Reading term one alone missed every derived term after the first."""

	def test_a_derived_term_that_is_not_first_is_still_detected(self):
		request = self._request(order_by=f"modified desc, {FIELD} asc")
		self.assertIn(FIELD, [f.fieldname for f in request.named])
		self.assertTrue(request.changes_the_query)

	def test_every_derived_term_is_rewritten_in_place_whatever_its_position(self):
		for order_by, expected in (
			(f"modified desc, {FIELD} asc", "modified desc, due_date asc"),
			(f"{FIELD} asc, modified desc", "due_date asc, modified desc"),
			(f"{FIELD} desc", "due_date desc"),
		):
			with self.subTest(order_by):
				self.assertEqual(self._request(order_by=order_by).for_native()["order_by"], expected)

	def test_a_second_term_naming_the_derived_field_really_orders_the_page(self):
		# Every fixture is created without a priority, so the first term ties and the second one decides.
		result = self._get_data(
			order_by=f"priority asc, {FIELD} asc",
			rows=_rows_arg("name", "priority", "due_date", FIELD),
		)
		dates = [r["due_date"] for r in result["data"] if r["due_date"]]
		self.assertEqual(dates, sorted(dates), "the trailing derived term did not resolve to due_date")

	def test_a_multi_term_sort_of_real_columns_is_left_exactly_as_the_caller_wrote_it(self):
		request = self._request(order_by="modified desc, due_date asc", rows=["name", FIELD])
		self.assertEqual(request.for_native()["order_by"], "modified desc, due_date asc")

	def test_a_multi_term_sort_of_real_columns_is_answered_by_native_byte_for_byte(self):
		self._identical_to_native(order_by="modified desc, due_date asc")


class TestASortWithNoProxyNeverReachesSQL(TranslationCase):
	"""A declaration that names no `order_by` proxy has nothing to sort on, and the derived name must not
	survive into SQL — frappe answers `PermissionError: You do not have permission to access field`.
	Batch 3 makes such a declaration inadmissible; until it does, the engine may not rely on that."""

	def setUp(self):
		super().setUp()
		self.unsortable = derived.register(
			derived.DerivedField(
				doctype=TASK,
				fieldname="_probe_unsortable",
				label="Unsortable Probe",
				buckets=[
					derived.Bucket("Closed", [("status", "in", fields.CLOSED)]),
					derived.Bucket("Open", [("status", "not in", fields.CLOSED)]),
				],
			)
		)

	def tearDown(self):
		derived._REGISTRY.get(TASK, {}).pop("_probe_unsortable", None)
		derived._validated().discard(TASK)
		super().tearDown()

	def test_a_term_with_no_proxy_is_dropped_and_the_framework_default_stands(self):
		name = self.unsortable.fieldname
		self.assertIsNone(self._request(order_by=f"{name} asc").for_native()["order_by"])
		self.assertEqual(
			self._request(order_by=f"modified desc, {name} asc").for_native()["order_by"], "modified desc"
		)

	def test_sorting_by_it_still_answers_the_rep_a_list(self):
		result = self._get_data(order_by=f"{self.unsortable.fieldname} asc")
		self.assertEqual(len(result["data"]), len(self.names))


class TestTheBoardEnvelopeIsHonest(TranslationCase):
	"""Item #6. The shell is fetched with `page_length=1` to buy a cheap count, and the board never wrote
	the caller's own paging back — so `ViewControls.vue:456` read 1 and the next view asked for one row."""

	BOARD: typing.ClassVar[dict] = {"view": {"view_type": "kanban"}, "column_field": FIELD}

	def test_a_derived_board_reports_the_callers_page_length_not_the_shells(self):
		result = self._get_data(**self.BOARD)
		self.assertEqual(result["page_length"], 50)
		self.assertEqual(result["page_length_count"], 50)

	def test_a_derived_board_row_count_is_the_rows_it_actually_returned(self):
		result = self._get_data(**self.BOARD)
		served = sum(len(column["data"]) for column in result["data"])
		self.assertEqual(result["row_count"], served)
		self.assertGreater(served, 1, "the fixtures did not reach the board at all")

	def test_a_derived_board_total_is_its_columns_summed_not_the_shells_own_count(self):
		# The shell is narrowed by the DERIVED-FREE filters only, so a board that also carries a derived
		# filter counted every row in scope while showing two — "7 of 7" over a board holding 2.
		result = self._get_data(**self.BOARD, filters={"title": ["like", f"{PROBE}%"], FIELD: "Overdue"})
		self.assertEqual(
			result["total_count"], sum(column["all_count"] for column in result["kanban_columns"])
		)
		self.assertEqual(result["total_count"], 2)

	def test_a_real_column_board_is_answered_by_native_byte_for_byte(self):
		self._identical_to_native(view={"view_type": "kanban"}, column_field="status")
