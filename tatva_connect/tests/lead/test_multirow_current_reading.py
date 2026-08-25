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

	def test_zero_and_false_are_answers(self):
		"""A dosage of 0 and an unticked Check are recorded facts. Calling them blank would carry a stale
		number forward over a real one — the whole failure this change exists to prevent, inverted."""
		for value in (0, 0.0, False):
			with self.subTest(value=value):
				self.assertFalse(multirow.is_blank(value))

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

	def test_a_stored_zero_on_the_newest_row_is_not_a_hole(self):
		rows = _rows(("2026-01-01", "old", {"utm_source": 7}),
		             ("2026-06-01", "new", {"utm_source": 0}))
		self.assertEqual(multirow.current_values(rows, KEY)["utm_source"], 0)

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


class TestSqlMirrorsPython(FrappeTestCase):
	"""The rule's only other rendering. It is generated from `multirow.order_keys`, so the ordering cannot
	drift; what CAN drift is the notion of blank, which is why it is asserted here in both directions."""

	def _sql(self, doctype, column):
		"""Rendered INSIDE a query, the way the composer renders it — a bare term quotes nothing, so
		asserting on one would assert about a shape production never emits."""
		from frappe.query_builder import DocType

		from tatva_connect.smartview import api

		inner = DocType(doctype).as_("_tc_src")
		window = api._current_column(inner, doctype, column, KEY)
		return frappe.qb.from_(inner).select(window.as_("v")).get_sql().lower()

	def test_it_is_a_first_value_window_partitioned_by_parent(self):
		"""FIRST_VALUE and not LAST_VALUE: with an ORDER BY the default frame ends at the current row, so
		FIRST_VALUE reads the partition's first row and needs no frame clause to be correct."""
		sql = self._sql("CRM Acquisition Profile", FIELDNAME)
		self.assertIn("first_value", sql)
		self.assertIn("partition by `_tc_src`.`parent`", sql)

	def test_blank_rows_sort_last_then_the_shared_ordering(self):
		sql = self._sql("CRM Acquisition Profile", FIELDNAME)
		self.assertIn("case when", sql, "blank-last is the first ordering term")
		for field in multirow.order_keys(KEY):
			self.assertIn(f"`{field}` desc", sql)

	def test_every_column_in_the_window_is_table_qualified(self):
		"""RED before the fix, and fatal: unqualified, an ordering column resolves to the SELECT alias of
		the same name — itself a window — and MariaDB raises 4016. A view selecting its own row key hit it."""
		sql = self._sql("CRM Acquisition Profile", KEY)
		self.assertNotIn("order by case when `touch_at`", sql, "the CASE must name the source table")
		self.assertIn("`_tc_src`.`touch_at`", sql)

	def test_a_text_column_is_blank_as_null_or_empty_string(self):
		self.assertIn("=''", self._sql("CRM Acquisition Profile", FIELDNAME))

	def test_a_number_is_blank_only_as_null(self):
		"""MariaDB coerces '' to zero, so testing it on a number would call a stored 0 empty — exactly what
		`is_blank` refuses. RED if the SQL ever tests emptiness the same way on every fieldtype."""
		doctype, column = "CRM Lab Profile", "weight_kg"
		self.assertIn(frappe.get_meta(doctype).get_field(column).fieldtype,
		              ("Float", "Int", "Currency", "Percent", "Long Int", "Check"))
		self.assertNotIn("=''", self._sql(doctype, column))

	def test_the_database_and_python_agree_over_real_rows(self):
		"""The one that would catch a real divergence: every multi-row section on this bench that has a lead
		with more than one row, read both ways, column by column. Not a fixture — the shapes that actually
		exist (a blank text cell, a stored zero, a null date) are exactly what a hand-built row would miss."""
		from frappe.model import NO_VALUE_FIELDS
		from frappe.query_builder import DocType

		from tatva_connect.smartview import api

		checked = []
		for section in frappe.get_all("CRM Lead Section", filters={"is_multi_row": 1},
		                              fields=["name", "target_doctype", "row_key_field"]):
			doctype, key = section.target_doctype, section.row_key_field
			if not (doctype and key):
				continue
			parents = [r[0] for r in frappe.db.sql(
				"""select parent from `tab{0}` where parenttype = 'CRM Lead'
				   group by parent having count(*) > 1 limit 10""".format(doctype))]  # sqli-ok: a doctype name
			if not parents:
				continue
			columns = [df.fieldname for df in frappe.get_meta(doctype).fields
			           if df.fieldtype not in NO_VALUE_FIELDS][:8]
			if not columns:
				continue
			inner = DocType(doctype).as_("_tc_src")
			query = (
				frappe.qb.from_(inner)
				.select(inner.parent,
				        *[api._current_column(inner, doctype, col, key).as_(col) for col in columns])
				.where((inner.parenttype == "CRM Lead") & inner.parent.isin(parents))
			)
			from_sql = {r["parent"]: r for r in frappe.db.sql(query.get_sql(), as_dict=True)}
			for parent, sql_row in from_sql.items():
				rows = frappe.get_all(doctype, filters={"parent": parent, "parenttype": "CRM Lead"},
				                      fields=["name", "creation", key, *columns], limit_page_length=0)
				current = multirow.current_values(rows, key)
				for col in columns:
					with self.subTest(doctype=doctype, parent=parent, column=col):
						self.assertEqual(sql_row[col], current[col],
						                 "the window and the sorter must read the same answer")
			checked.append(doctype)
		if not checked:
			self.skipTest("no lead on this bench keeps two rows of any multi-row section")
