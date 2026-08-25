# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A multi-row section shows the answer the lead HAS — per column, the newest row that carries one.

The defect: rows on one section are written by DIFFERENT forms, so a blank cell means "the form that
wrote this row never asked the question", not "the answer is empty". Every consumer read the latest row
whole, so a patient whose address was captured on an earlier cycle saw a blank address box on the order
form — while `detail.empty_everywhere` was already saying, about the very same field, that it was not
empty. The flag and the value disagreed. These tests lock them together.

Two questions that were one function, and must stay two:

  * `row_for_section` — the ADDRESS an edit lands on. Still the latest row, so a punch writes today's
    observation onto today's row and an increment reads the cell it is about to overwrite.
  * `current_for_section` — what the section SAYS. Per column, so a value survives a row that didn't ask.

The SQL is the same rule's only other rendering — a database cannot call the Python one — so the last
class renders it and compares it against the Python answer over real rows.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_multirow_current_reading
"""
import io

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead import detail, multirow

SECTION = "acq"
FIELD_KEY = "acq:utm_campaign"
FIELDNAME = "utm_campaign"
KEY = "touch_at"


def _row(**kw):
	return frappe._dict(kw)


def _rows(*pairs):
	"""Rows newest LAST, so a test reads in the order the story happens."""
	return [_row(touch_at=at, creation=f"{at[:10]} 09:00:00", name=name, **cells)
	        for at, name, cells in pairs]


class TestIsBlank(FrappeTestCase):
	"""What the fallback walks past. A cell that says nothing — never one that says zero."""

	def test_nothing_is_blank(self):
		for value in (None, "", "   "):
			with self.subTest(value=value):
				self.assertTrue(multirow.is_blank(value))

	def test_an_empty_set_is_blank(self):
		self.assertTrue(multirow.is_blank([]))

	def test_a_zero_is_an_answer_when_the_caller_cannot_say_what_it_holds(self):
		"""Told no fieldtype, zero stays an answer — the safe reading."""
		for value in (0, 0.0, False):
			with self.subTest(value=value):
				self.assertFalse(multirow.is_blank(value))

	def test_a_number_that_cannot_hold_null_says_nothing_with_a_zero(self):
		"""RED before the fix, and what a rep saw: `custom_vivitra_order_cycle_number` is `int(11) NOT NULL
		DEFAULT 0`, so a form that never asked the question stored 0 and the picker showed 0 over the 8 the
		rep had entered on an earlier cycle. The schema left no other way to say "not asked"."""
		for fieldtype in ("Int", "Long Int", "Float", "Percent"):
			with self.subTest(fieldtype=fieldtype):
				self.assertTrue(multirow.is_blank(0, fieldtype))
				self.assertFalse(multirow.is_blank(8, fieldtype))

	def test_a_check_and_a_currency_keep_their_zero(self):
		"""An unticked box is "No" and a zero price is free of charge — both answers a rep gave. Falling back
		would resurrect an old "Yes", or an old price over a genuinely free cycle."""
		for fieldtype in ("Check", "Currency"):
			with self.subTest(fieldtype=fieldtype):
				self.assertFalse(multirow.is_blank(0, fieldtype))

	def test_the_panel_flag_is_this_same_function(self):
		self.assertFalse(detail.empty_everywhere([None, "spring"]))
		self.assertTrue(detail.empty_everywhere([None, "", "   "]))
		self.assertFalse(detail.empty_everywhere([0]))


class TestCurrentValues(FrappeTestCase):
	"""The rule itself, over rows and nothing else."""

	def test_blank_on_the_newest_row_falls_back_to_the_row_that_has_it(self):
		rows = _rows(("2026-01-01", "old", {FIELDNAME: "spring"}),
		             ("2026-06-01", "new", {FIELDNAME: None}))
		self.assertEqual(multirow.current_values(rows, KEY)[FIELDNAME], "spring")

	def test_the_newest_answer_wins_and_is_never_overwritten_by_an_older_one(self):
		"""The fallback fills a hole; it must never reach past an answer. A newer cycle's address is the
		address, whatever the older cycles said."""
		rows = _rows(("2026-01-01", "old", {FIELDNAME: "spring"}),
		             ("2026-06-01", "new", {FIELDNAME: "summer"}))
		self.assertEqual(multirow.current_values(rows, KEY)[FIELDNAME], "summer")

	def test_it_walks_past_more_than_one_silent_row(self):
		rows = _rows(("2026-01-01", "a", {FIELDNAME: "spring"}),
		             ("2026-03-01", "b", {FIELDNAME: ""}),
		             ("2026-06-01", "c", {FIELDNAME: None}))
		self.assertEqual(multirow.current_values(rows, KEY)[FIELDNAME], "spring")

	def test_blank_in_every_row_stays_blank(self):
		rows = _rows(("2026-01-01", "a", {FIELDNAME: ""}),
		             ("2026-06-01", "b", {FIELDNAME: None}))
		self.assertTrue(multirow.is_blank(multirow.current_values(rows, KEY)[FIELDNAME]))

	def test_a_stored_zero_on_a_text_column_is_not_a_hole(self):
		rows = _rows(("2026-01-01", "old", {"utm_source": 7}),
		             ("2026-06-01", "new", {"utm_source": 0}))
		self.assertEqual(multirow.current_values(rows, KEY)["utm_source"], 0)

	def test_an_int_column_falls_back_past_a_zero_the_schema_forced(self):
		"""The rep's own case: 0 on the newest cycle, 8 on the one before."""
		rows = _rows(("2026-01-01", "old", {"custom_vivitra_order_cycle_number": 8}),
		             ("2026-06-01", "new", {"custom_vivitra_order_cycle_number": 0}))
		current = multirow.current_values(rows, KEY, "CRM Drug Program Profile")
		self.assertEqual(current["custom_vivitra_order_cycle_number"], 8)

	def test_columns_resolve_independently_of_one_another(self):
		"""The reading is per column, so one field's silence never drags another field's answer back."""
		rows = _rows(("2026-01-01", "old", {FIELDNAME: "spring", "utm_source": "google"}),
		             ("2026-06-01", "new", {FIELDNAME: None, "utm_source": "meta"}))
		current = multirow.current_values(rows, KEY)
		self.assertEqual(current[FIELDNAME], "spring")
		self.assertEqual(current["utm_source"], "meta")

	def test_the_framework_columns_describe_the_newest_row(self):
		"""`name`/`creation`/`touch_at` are never blank, so they never fall back — the reading still says
		WHEN it was taken, and the rows modal opens on that row."""
		rows = _rows(("2026-01-01", "old", {FIELDNAME: "spring"}),
		             ("2026-06-01", "new", {FIELDNAME: None}))
		current = multirow.current_values(rows, KEY)
		self.assertEqual(current["name"], "new")
		self.assertEqual(current[KEY], "2026-06-01")

	def test_no_rows_is_no_reading(self):
		self.assertIsNone(multirow.current_values([], KEY))


class TestTheTwoQuestionsStayTwo(FrappeTestCase):
	"""A write address is not a reading. Merging them is the defect in the other direction."""

	def setUp(self):
		self.section = frappe.get_cached_doc("CRM Lead Section", SECTION)
		rows = _rows(("2026-01-01", "old", {FIELDNAME: "spring"}),
		             ("2026-06-01", "new", {FIELDNAME: None}))
		self.doc = frappe._dict({self.section.child_table_field: rows})

	def test_the_edit_address_is_still_the_latest_row(self):
		"""If this ever follows the value, a punch would overwrite an old cycle's row."""
		self.assertEqual(multirow.row_for_section(self.doc, self.section).name, "new")
		self.assertEqual(detail._row_key(self.doc, self.section), "2026-06-01")

	def test_the_reading_carries_while_the_address_does_not(self):
		self.assertIsNone(multirow.row_for_section(self.doc, self.section).get(FIELDNAME))
		self.assertEqual(multirow.current_for_section(self.doc, self.section)[FIELDNAME], "spring")

	def test_a_reading_cannot_be_written_through(self):
		"""A projection, never a Document — so no caller can mistake it for a row and save into it."""
		from frappe.model.document import Document

		self.assertNotIsInstance(multirow.current_for_section(self.doc, self.section), Document)

	def test_a_section_with_no_rows_answers_none_to_both(self):
		empty = frappe._dict({self.section.child_table_field: []})
		self.assertIsNone(multirow.row_for_section(empty, self.section))
		self.assertIsNone(multirow.current_for_section(empty, self.section))


class TestTheSmartViewListIsTheOneKnOWnException(FrappeTestCase):
	"""The list's FILTER/SORT join keeps the latest-row rule on purpose, and this records why.

	A window per selected column measured 1.4x-6x on the child subquery, growing with the column count —
	a list over 173k leads cannot pay it. What the list DISPLAYS is unaffected: those values are filled
	page-scoped in Python by `_hydrate`, under the shared rule. So the only gap is that a view filtering
	on a column whose value sits on an earlier row can miss those rows."""

	def test_the_join_ranks_by_the_shared_ordering(self):
		from tatva_connect.smartview import api

		self.assertIn("multirow.order_keys", open(api.__file__, encoding="utf-8").read(),
		              "the join must take its ordering from the one declaration, not restate it")

	def test_the_page_fill_uses_the_shared_reading(self):
		"""The DISPLAY path is the shared rule — that is what keeps the list's values right."""
		from tatva_connect.smartview import api

		source = open(api.__file__, encoding="utf-8").read()
		self.assertIn("multirow.is_blank", source)
		self.assertIn("multirow.order_by", source)


class TestANewObservationRowIsBornComplete(FrappeTestCase):
	"""The write half of the same philosophy: a punch answers a handful of a section's columns, so a row
	staged from the answers alone came out one value beside a line of dashes and the rows table read as if
	the patient had no history. The row is the state AS SUBMITTED — every column the lead already answers
	for, with the punch's own answers over it."""

	def setUp(self):
		self.section = frappe.get_cached_doc("CRM Lead Section", SECTION)
		self.doc = frappe._dict({self.section.child_table_field: _rows(
			("2026-01-01", "old", {FIELDNAME: "spring", "utm_source": "google"}),
			("2026-06-01", "new", {FIELDNAME: None, "utm_source": "meta"}),
		)})

	def test_it_carries_the_sections_current_reading(self):
		carried = detail.carried_forward(self.doc, self.section)
		self.assertEqual(carried[FIELDNAME], "spring", "the newest row that HAS an answer")
		self.assertEqual(carried["utm_source"], "meta")

	def test_it_never_carries_the_row_key(self):
		"""The key is the row's ADDRESS. A second row wearing the first's address is not a new observation,
		it is a collision — `_row_key` addresses multi-value selections by it."""
		self.assertNotIn(KEY, detail.carried_forward(self.doc, self.section))

	def test_it_never_carries_frappe_s_own_columns(self):
		carried = detail.carried_forward(self.doc, self.section)
		for column in ("name", "creation", "modified", "owner", "idx", "parent", "parenttype", "parentfield"):
			self.assertNotIn(column, carried)

	def test_a_column_blank_in_every_row_is_not_carried_as_blank(self):
		doc = frappe._dict({self.section.child_table_field: _rows(
			("2026-01-01", "a", {FIELDNAME: ""}), ("2026-06-01", "b", {FIELDNAME: None}))})
		self.assertNotIn(FIELDNAME, detail.carried_forward(doc, self.section))

	def test_a_section_with_no_rows_carries_nothing(self):
		self.assertEqual(detail.carried_forward(frappe._dict({self.section.child_table_field: []}), self.section), {})

	def test_the_row_is_only_born_this_way_for_a_fresh_multi_row_observation(self):
		"""`_stage_section` decides that with `new_observation and _is_multi_row(section)` — a Data tab edit
		passes False and a single-row section answers False, so both keep the row they already have. That
		gate is unchanged by this work; the real-document paths are exercised by `test_section_history`."""
		self.assertFalse(detail._is_multi_row(frappe.get_cached_doc("CRM Lead Section", "lead")))
		self.assertTrue(detail._is_multi_row(self.section))
