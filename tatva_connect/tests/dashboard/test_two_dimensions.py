# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A second dimension, a month bucket, a derived group and a distinct count — added without moving anything.

The whole risk of this extension is REGRESSION, not the new shapes: every card on every dashboard already
reads `points`, and the four additions all pass through the same executor. So the first thing proved here
is that a single-dimension card is untouched, key for key, and the rest is what the additions must each do:

  * A SINGLE-DIMENSION CARD IS BYTE FOR BYTE WHAT IT WAS. No `series`, the same point keys, the same drill.
  * A MONTH BUCKET IS AN AXIS, taken through frappe's own `{"MONTH": column, "as": alias}` and grouped on
    the alias — the one form proven to reach SQL through `get_list`.
  * A MONTH POINT CARRIES NO DRILL. `bucket` is a select alias no column takes, and MONTH answers 1-12
    with the year folded away, so no filter and no date range expresses it. A drill nobody should follow
    is worse than none, and this is the same rule an undrillable card already obeys.
  * BOTH DIMENSIONS COME OUT OF ONE QUERY, pivoted in Python. The axis totals are the cells the card really
    drew, so a stack and its own axis always add up, and a CELL drill carries BOTH dimensions or neither.
  * THE SERIES COUNT IS CAPPED BY A DECLARED CONSTANT, largest first, so a split with two hundred values
    is a bounded chart rather than an unbounded legend.
  * A DERIVED GROUP IS ONE GATED COUNT PER BUCKET. It is not a column, so `group_by` cannot reach it; each
    bucket already IS a frappe filter list, and the drill names the FIELD — the list engine translates it,
    where a bucket flattened into a filter dict would lose its second term on one column.
  * A DISTINCT COUNT IS A NUMBER OF GROUPS. There is no COUNT DISTINCT, and none is needed.

Tasks are the fixture list for the same reason `test_executor` uses them: a task is creatable from
three columns, and the properties under test are the executor's.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.dashboard.test_two_dimensions
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, getdate, now_datetime, nowdate

from tatva_connect.dashboard import declaration, executor
from tatva_connect.list_engine import derived

TASK = "CRM Task"
PROBE = "SplitProbe"
USER_ONE = "probe-split-one@tatvacare.test"
USER_TWO = "probe-split-two@tatvacare.test"

# Declared here rather than leaned on, so a site whose operator authored none still proves the mechanism.
STATE = "_probe_split_state"


def _chart(**overrides):
	"""A minimal sound declaration over the probe rows, so each test varies one thing."""
	chart = {
		"chart_name": "probe_split_card",
		"label": "Probe Split Card",
		"subtitle": "",
		"chart_type": "bar",
		"source_doctype": TASK,
		"aggregate": "COUNT",
		"aggregate_field": "",
		"group_by_field": "status",
		"split_by": "",
		"time_bucket": declaration.NO_BUCKET,
		"date_field": "creation",
		"honours_date_range": 0,
		"base_filters": '{"title": ["like", "' + PROBE + '%"]}',
		"row_limit": 10,
		"drill_enabled": 1,
	}
	chart.update(overrides)
	return chart


class SplitCase(FrappeTestCase):
	"""Five tasks across two statuses and three assignees, so every cell of the cross has a known figure.

	    Todo  x one=2  two=1  none=0
	    Done  x one=0  two=1  none=1
	"""

	def setUp(self):
		frappe.set_user("Administrator")
		self._clear()
		for email, first in ((USER_ONE, "One"), (USER_TWO, "Two")):
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": f"Split {first}",
						"send_welcome_email": 0,
					}
				).insert(ignore_permissions=True)
		self.rows = [
			("todo_one_a", "Todo", USER_ONE),
			("todo_one_b", "Todo", USER_ONE),
			("todo_two", "Todo", USER_TWO),
			("done_two", "Done", USER_TWO),
			("done_none", "Done", None),
		]
		self.names = []
		for label, status, assignee in self.rows:
			doc = frappe.get_doc(
				{
					"doctype": TASK,
					"title": f"{PROBE} {label}",
					"status": status,
					"assigned_to": assignee,
				}
			).insert(ignore_permissions=True)
			self.names.append(doc.name)

	def tearDown(self):
		self._clear()

	def _clear(self):
		frappe.db.delete(TASK, {"title": ["like", f"{PROBE}%"]})
		frappe.db.delete("User", {"name": ["in", [USER_ONE, USER_TWO]]})

	def _by_raw(self, points):
		return {point["raw"]: point for point in points}

	def _series(self, payload, raw):
		return next(one for one in payload["series"] if one["raw"] == raw)


class TestASingleDimensionCardDidNotMove(SplitCase):
	def test_a_single_dimension_card_carries_no_series_at_all(self):
		"""Catches `series` becoming unconditional, which every existing card's reader would then have to know."""
		self.assertNotIn("series", executor.run(_chart()))

	def test_a_card_has_exactly_the_keys_it_always_had(self):
		payload = executor.run(_chart())
		self.assertEqual(
			sorted(payload), ["chart", "drill", "label", "points", "subtitle", "type", "value"]
		)

	def test_a_point_has_exactly_the_keys_it_always_had(self):
		"""Catches a key added to every point in the app for the sake of the two cards that need one."""
		payload = executor.run(_chart())
		self.assertEqual(sorted(payload["points"][0]), ["drill", "label", "raw", "value"])

	def test_the_figures_are_unchanged(self):
		points = self._by_raw(executor.run(_chart())["points"])
		self.assertEqual(points["Todo"]["value"], 3)
		self.assertEqual(points["Done"]["value"], 2)

	def test_the_drill_still_carries_the_grouped_value(self):
		points = self._by_raw(executor.run(_chart())["points"])
		self.assertEqual(points["Done"]["drill"]["filters"]["status"], "Done")


class TestAMonthBucketIsAnAxis(SplitCase):
	"""Two of the five are backdated a month, so the axis really has two buckets to tell apart.

	`creation` is moved by a plain write rather than declared at insert: whether frappe honours a preset
	creation has varied by version, and this suite is about the executor, not about that."""

	def setUp(self):
		super().setUp()
		self.last_month = add_to_date(nowdate(), months=-1)
		self.this = getdate(nowdate()).month
		self.previous = getdate(self.last_month).month
		for name in self.names[:2]:
			frappe.db.set_value(
				TASK, name, "creation", f"{self.last_month} 09:00:00", update_modified=False
			)

	def test_the_axis_is_the_month_of_the_declared_date_column(self):
		"""Catches a bucket that never reaches SQL: the whole card would group by nothing and read as one bar."""
		payload = executor.run(
			_chart(chart_type="line", group_by_field="", time_bucket=declaration.MONTH)
		)
		self.assertEqual({point["raw"] for point in payload["points"]}, {self.this, self.previous})

	def test_each_month_counts_only_its_own_rows(self):
		payload = executor.run(
			_chart(chart_type="line", group_by_field="", time_bucket=declaration.MONTH)
		)
		points = self._by_raw(payload["points"])
		self.assertEqual(points[self.previous]["value"], 2)
		self.assertEqual(points[self.this]["value"], 3)

	def test_a_month_point_carries_no_drill(self):
		"""Catches a drill on `bucket`, which is a select alias no list can filter on."""
		payload = executor.run(
			_chart(chart_type="line", group_by_field="", time_bucket=declaration.MONTH)
		)
		self.assertTrue(all("drill" not in point for point in payload["points"]))

	def test_the_card_itself_is_still_drillable(self):
		"""The card opens its own list; only the per-month point cannot be expressed as a filter."""
		payload = executor.run(
			_chart(chart_type="line", group_by_field="", time_bucket=declaration.MONTH)
		)
		self.assertEqual(payload["drill"]["doctype"], TASK)

	def test_the_total_is_the_whole_card_and_not_one_bucket(self):
		payload = executor.run(
			_chart(chart_type="line", group_by_field="", time_bucket=declaration.MONTH)
		)
		self.assertEqual(payload["value"], 5)


class TestTwoDimensionsComeOutOfOneQuery(SplitCase):
	def _split(self, **overrides):
		return executor.run(_chart(chart_type="stacked_bar", split_by="assigned_to", **overrides))

	def test_a_split_card_still_carries_the_axis_as_points(self):
		"""Catches `points` being replaced by `series`, which would blank every reader that predates the split."""
		points = self._by_raw(self._split()["points"])
		self.assertEqual(points["Todo"]["value"], 3)
		self.assertEqual(points["Done"]["value"], 2)

	def test_there_is_one_series_per_split_value(self):
		payload = self._split()
		self.assertEqual({one["raw"] for one in payload["series"]}, {USER_ONE, USER_TWO, ""})

	def test_a_cell_holds_the_figure_for_both_dimensions(self):
		payload = self._split()
		cells = self._by_raw(self._series(payload, USER_ONE)["points"])
		self.assertEqual(cells["Todo"]["value"], 2)

	def test_a_cell_the_query_returned_no_row_for_is_zero_and_not_missing(self):
		"""Catches a ragged series: AxisChart reads a row per axis value, and a hole shifts every bar after it."""
		payload = self._split()
		cells = self._by_raw(self._series(payload, USER_ONE)["points"])
		self.assertEqual(sorted(cells), sorted(point["raw"] for point in payload["points"]))
		self.assertEqual(cells["Done"]["value"], 0)

	def test_the_axis_total_is_the_sum_of_the_cells_drawn_under_it(self):
		"""Catches an axis figure counted separately from the stack, which reads as a bar that does not add up."""
		payload = self._split()
		for point in payload["points"]:
			drawn = sum(
				self._by_raw(one["points"])[point["raw"]]["value"] for one in payload["series"]
			)
			self.assertEqual(point["value"], drawn)

	def test_a_cell_drills_on_both_dimensions(self):
		payload = self._split()
		cell = self._by_raw(self._series(payload, USER_TWO)["points"])["Done"]
		self.assertEqual(cell["drill"]["filters"]["status"], "Done")
		self.assertEqual(cell["drill"]["filters"]["assigned_to"], USER_TWO)

	def test_a_blank_cell_drills_on_the_blank_it_really_had(self):
		"""The words the reader saw must never reach the filter — `Not set` is not a value anything holds."""
		payload = self._split()
		cell = self._by_raw(self._series(payload, "")["points"])["Done"]
		self.assertEqual(cell["drill"]["filters"]["assigned_to"], ["is", "not set"])

	def test_a_split_over_a_month_axis_drills_nowhere(self):
		"""Half a filter is worse than none: the split alone would open every month at once."""
		payload = self._split(group_by_field="", time_bucket=declaration.MONTH)
		self.assertTrue(
			all("drill" not in cell for one in payload["series"] for cell in one["points"])
		)

	def test_the_card_total_is_its_own_query_and_not_the_sum_of_the_cells(self):
		self.assertEqual(self._split()["value"], 5)


class TestTheSeriesCountIsCapped(SplitCase):
	"""The cap is proved on the picker rather than on eleven fixture users: what is under test is that a
	DECLARED constant bounds the series and that the largest survive, and both are decided right here."""

	def test_the_declared_cap_really_bounds_the_query(self):
		"""Catches a cap that is declared and never applied. The fixture holds fewer assignees than
		SERIES_LIMIT, so asserting `<= 8` would pass with no cap at all — the bound is driven directly."""
		chart = frappe._dict(_chart(split_by="assigned_to"))
		scope = {"title": ["like", f"{PROBE}%"]}
		self.assertGreater(len(executor._groups(chart, scope, "assigned_to", 10)), 1)
		self.assertEqual(len(executor._groups(chart, scope, "assigned_to", 1)), 1)

	def test_the_series_are_ranked_largest_first(self):
		"""Catches a cap that keeps whatever SQL returned first rather than the biggest stacks. Asserted as
		an ordering and not a named winner: the two probe users hold two tasks each, so a tie has no
		deterministic winner and naming one would be asserting the tie-break, not the ranking."""
		chart = frappe._dict(_chart(split_by="assigned_to"))
		scope = {"title": ["like", f"{PROBE}%"]}
		rows = executor._groups(chart, scope, "assigned_to", 10)
		values = [row["value"] for row in rows]
		self.assertEqual(values, sorted(values, reverse=True), "series were not ranked by their own measure")
		self.assertEqual(values[0], max(values))

	def test_the_cross_is_narrowed_to_the_series_that_are_kept(self):
		"""Catches the cross being bounded by an arithmetic guess instead of the series actually drawn: a
		guess lets SQL return the first months and silently drop the rest, and the card looks correct."""
		chart = frappe._dict(_chart(split_by="assigned_to"))
		self.assertEqual(
			executor._only_these_series(chart, [USER_ONE]), [["assigned_to", "in", [USER_ONE]]]
		)

	def test_a_blank_series_is_ored_in_because_in_cannot_match_null(self):
		"""Catches `in ['']` being used for a blank series, which matches no NULL row and drops the stack."""
		chart = frappe._dict(_chart(split_by="assigned_to"))
		terms = executor._only_these_series(chart, ["", USER_ONE])
		self.assertIn(["assigned_to", "is", "not set"], terms)
		self.assertIn(["assigned_to", "in", [USER_ONE]], terms)


class TestABlankAxisIsOneValueOnBothSides(SplitCase):
	"""Catches points and series disagreeing about blank: NULL and '' are two SQL groups and one fact, and
	the browser matches a cell to its axis point by `raw`, so two blanks orphan a cell."""

	def test_the_axis_and_its_series_read_a_blank_the_same_way(self):
		payload = executor.run(_chart(chart_type="stacked_bar", group_by_field="custom_task_type", split_by="assigned_to"))
		axis = [point["raw"] for point in payload["points"]]
		self.assertEqual(len(axis), len(set(axis)), "the axis carried the same value twice")
		for one in payload["series"]:
			self.assertEqual([cell["raw"] for cell in one["points"]], axis)


class TestAMonthReadsAsAMonth(SplitCase):
	"""Catches a month axis rendering the bare integer MariaDB returned — `1 2 3 7 8` on the x axis."""

	def test_a_month_point_is_named_not_numbered(self):
		payload = executor.run(_chart(chart_type="line", group_by_field="", time_bucket=declaration.MONTH))
		for point in payload["points"]:
			self.assertNotEqual(str(point["label"]), str(point["raw"]))
			self.assertTrue(point["label"].isalpha(), f"month label {point['label']!r} is not a name")


class TestADerivedGroupIsOneCountPerBucket(SplitCase):
	"""`Closed` is Done or Canceled and `Working` is neither, so the five rows split 2 / 3."""

	def setUp(self):
		super().setUp()
		derived.register(
			derived.DerivedField(
				doctype=TASK,
				fieldname=STATE,
				label="Probe Split State",
				buckets=[
					derived.Bucket("Closed", [("status", "in", ["Done", "Canceled"])]),
					derived.Bucket("Working", [("status", "not in", ["Done", "Canceled"])]),
				],
			)
		)
		self.addCleanup(derived._REGISTRY.get(TASK, {}).pop, STATE, None)
		self.addCleanup(derived.reload)

	def _derived(self, **overrides):
		return executor.run(_chart(chart_type="donut", group_by_field=STATE, **overrides))

	def test_a_derived_field_can_be_grouped_by_although_it_is_no_column(self):
		"""Catches `group_by=due_state` reaching SQL, which is `Unknown column` and a dead card."""
		payload = self._derived()
		self.assertEqual([point["raw"] for point in payload["points"]], ["Closed", "Working"])

	def test_each_bucket_counts_exactly_the_rows_it_claims(self):
		points = self._by_raw(self._derived()["points"])
		self.assertEqual(points["Closed"]["value"], 2)
		self.assertEqual(points["Working"]["value"], 3)

	def test_the_cards_own_filters_still_narrow_each_bucket(self):
		"""Catches a bucket counted across the whole table: the card would ignore its own base filters."""
		payload = self._derived(base_filters='{"title": ["like", "' + PROBE + ' done%"]}')
		points = self._by_raw(payload["points"])
		self.assertEqual(points["Closed"]["value"], 2)
		self.assertEqual(points["Working"]["value"], 0)

	def test_the_drill_names_the_derived_field_and_not_its_tuples(self):
		"""A bucket flattened into a filter dict loses its second term on one column; the list engine
		translates the field itself, so that is what the drill carries."""
		points = self._by_raw(self._derived()["points"])
		self.assertEqual(points["Closed"]["drill"]["filters"][STATE], "Closed")

	def test_the_drill_still_carries_the_cards_own_narrowing(self):
		points = self._by_raw(self._derived()["points"])
		self.assertEqual(points["Closed"]["drill"]["filters"]["title"], ["like", f"{PROBE}%"])

	def test_every_bucket_is_drawn_even_where_it_claims_nothing(self):
		"""A missing slice reads as a bucket that does not exist; an empty one reads as what it is."""
		payload = self._derived(base_filters='{"title": ["like", "' + PROBE + ' done%"]}')
		self.assertEqual(len(payload["points"]), 2)


class TestADistinctCountIsANumberOfGroups(SplitCase):
	def test_it_counts_how_many_different_values_the_column_holds(self):
		"""Catches a plain COUNT wearing the name. Blank is NOT a value — the axis, the series and this count
		all read NULL and '' as one non-answer, so two assignees and an unassigned row is two, not three."""
		payload = executor.run(
			_chart(
				chart_type="number",
				group_by_field="",
				aggregate=declaration.DISTINCT,
				aggregate_field="assigned_to",
			)
		)
		self.assertEqual(payload["value"], 2)

	def test_which_rows_count_is_still_the_declarations_to_say(self):
		"""Which ROWS count stays the declaration's; which VALUES are values does not — blank never is."""
		payload = executor.run(
			_chart(
				chart_type="number",
				group_by_field="",
				aggregate=declaration.DISTINCT,
				aggregate_field="assigned_to",
				base_filters='{"title": ["like", "'
				+ PROBE
				+ '%"], "assigned_to": ["is", "set"]}',
			)
		)
		self.assertEqual(payload["value"], 2)

	def test_it_is_a_whole_number_and_not_a_float(self):
		payload = executor.run(
			_chart(
				chart_type="number",
				group_by_field="",
				aggregate=declaration.DISTINCT,
				aggregate_field="assigned_to",
			)
		)
		self.assertIsInstance(payload["value"], int)


class TestAnEmptyResultIsStillTheCardsShape(SplitCase):
	"""A card that matches nothing answers with its own empty shape, not a missing key.

	This runs as Administrator and proves NOTHING about row gating — the gate is proved by
	tests/access/test_row_gate_one_answer.py, which drives a real scoped user."""

	def test_a_card_that_matches_nothing_still_carries_every_key(self):
		chart = _chart(
			chart_type="stacked_bar",
			split_by="assigned_to",
			base_filters='{"title": ["like", "NoSuchProbe%"]}',
		)
		payload = executor.run(chart)
		self.assertEqual(payload["points"], [])
		self.assertEqual(payload["series"], [])
		self.assertEqual(payload["value"], 0)
