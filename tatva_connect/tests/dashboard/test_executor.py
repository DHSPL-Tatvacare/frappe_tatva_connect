# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The declaration is the query, and the query is the drill-down. One thing, asserted from both ends.

The failure this file exists to prevent is the one that only ever surfaces in front of a user: a card
saying 247 that opens a list of 180. It cannot happen while the drill filter is DERIVED from the same
filters that produced the figure — so what is proved here is that derivation, at every point it could
diverge:

  * THE FIGURE IS THE GROUPED COUNT, and the declared filters really narrow it.
  * THE DATE RANGE APPLIES WHEN THE CARD SAYS IT DOES, and is absent when the card declares itself a
    snapshot. A dashboard that quietly mixed the two would be wrong in a way nobody could see.
  * `__now__` AND `__today__` MEAN REQUEST TIME. A stored filter cannot hold "now", and the whole of
    Overdue depends on this.
  * A BLANK GROUP IS NAMED IN PYTHON, because SQL's IFNULL cannot take a literal here — and the drill for
    that slice still filters on the blank it really had, NEVER on the words "Not set".
  * THE LABEL IS DISPLAY AND THE GROUP IS THE FILTER. `assigned_to.full_name` is a person's name; the
    drill has to carry their login or it returns nothing.
  * A CARD THAT IS NOT DRILLABLE CARRIES NO DRILL AT ALL, rather than one nobody should follow.

Activities are the fixture list because a task is creatable from three columns; the property under test is
the executor's and is not about which list it ran over.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.dashboard.test_executor
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime, nowdate

from tatva_connect.dashboard import executor

TASK = "CRM Task"
PROBE = "ExecProbe"
PROBE_USER = "probe-executor@tatvacare.test"

# The window every ranged assertion uses: today, which every probe row is created inside.
_TODAY = {"from_date": nowdate(), "to_date": nowdate()}


def _chart(**overrides):
	"""A minimal sound declaration over the probe rows, so each test varies one thing."""
	chart = {
		"chart_name": "probe_exec_card",
		"label": "Probe Card",
		"subtitle": "",
		"chart_type": "bar",
		"source_doctype": TASK,
		"aggregate": "COUNT",
		"aggregate_field": "",
		"group_by_field": "status",
		"label_field": "",
		"date_field": "creation",
		"honours_date_range": 0,
		"base_filters": '{"title": ["like", "' + PROBE + '%"]}',
		"row_limit": 10,
		"drill_enabled": 1,
	}
	chart.update(overrides)
	return chart


class ExecutorCase(FrappeTestCase):
	"""Seven activities with a known distribution, scoped by title so every count is deterministic."""

	def setUp(self):
		frappe.set_user("Administrator")
		self._clear()
		if not frappe.db.exists("User", PROBE_USER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": PROBE_USER,
					"first_name": "Exec",
					"last_name": "Probe",
					"send_welcome_email": 0,
				}
			).insert(ignore_permissions=True)
		now = now_datetime()
		self.rows = [
			("todo_overdue", "Todo", add_to_date(now, days=-2), PROBE_USER),
			("todo_upcoming", "Todo", add_to_date(now, days=2), PROBE_USER),
			("todo_no_due", "Todo", None, None),
			("progress_overdue", "In Progress", add_to_date(now, days=-1), None),
			("done_one", "Done", add_to_date(now, days=-3), None),
			("done_two", "Done", add_to_date(now, days=-4), None),
			("canceled_one", "Canceled", None, None),
		]
		for label, status, due, assignee in self.rows:
			frappe.get_doc(
				{
					"doctype": TASK,
					"title": f"{PROBE} {label}",
					"status": status,
					"due_date": due,
					"assigned_to": assignee,
				}
			).insert(ignore_permissions=True)

	def tearDown(self):
		self._clear()

	def _clear(self):
		frappe.db.delete(TASK, {"title": ["like", f"{PROBE}%"]})
		frappe.db.delete("User", {"name": PROBE_USER})

	def _by_raw(self, payload):
		return {point["raw"]: point for point in payload["points"]}


class TestTheFigureIsTheDeclaration(ExecutorCase):
	def test_a_grouped_count_groups_by_the_declared_column(self):
		points = self._by_raw(executor.run(_chart()))
		self.assertEqual(points["Todo"]["value"], 3)
		self.assertEqual(points["Done"]["value"], 2)
		self.assertEqual(points["In Progress"]["value"], 1)

	def test_the_card_total_is_the_sum_of_its_points(self):
		payload = executor.run(_chart())
		self.assertEqual(payload["value"], 7)

	def test_base_filters_narrow_the_card(self):
		payload = executor.run(
			_chart(base_filters='{"title": ["like", "' + PROBE + '%"], "status": "Done"}')
		)
		self.assertEqual(payload["value"], 2)

	def test_a_number_card_is_one_figure_and_no_points(self):
		payload = executor.run(_chart(chart_type="number", group_by_field=""))
		self.assertEqual(payload["value"], 7)
		self.assertEqual(payload["points"], [])

	def test_the_row_limit_caps_the_points(self):
		payload = executor.run(_chart(row_limit=2))
		self.assertEqual(len(payload["points"]), 2)


class TestTheDateRangeIsADeclaredChoice(ExecutorCase):
	def test_a_ranged_card_answers_for_the_window(self):
		payload = executor.run(_chart(chart_type="number", group_by_field="", honours_date_range=1), _TODAY)
		self.assertEqual(payload["value"], 7)

	def test_a_ranged_card_excludes_what_is_outside_the_window(self):
		last_year = {"from_date": add_to_date(nowdate(), years=-1), "to_date": add_to_date(nowdate(), days=-90)}
		payload = executor.run(
			_chart(chart_type="number", group_by_field="", honours_date_range=1), last_year
		)
		self.assertEqual(payload["value"], 0)

	def test_a_snapshot_card_ignores_the_window_entirely(self):
		"""Overdue means overdue now. A card that says so must not be quietly dated by the range."""
		last_year = {"from_date": add_to_date(nowdate(), years=-1), "to_date": add_to_date(nowdate(), days=-90)}
		payload = executor.run(
			_chart(chart_type="number", group_by_field="", honours_date_range=0), last_year
		)
		self.assertEqual(payload["value"], 7)


class TestTheTokensMeanRequestTime(ExecutorCase):
	def test_now_is_substituted_before_the_query(self):
		"""Two of the seven are open and already past due; a stored `__now__` proves it is read on the way in."""
		payload = executor.run(
			_chart(
				chart_type="number",
				group_by_field="",
				base_filters='{"title": ["like", "'
				+ PROBE
				+ '%"], "status": ["in", ["Backlog", "Todo", "In Progress"]], "due_date": ["between", ["1900-01-01", "__now__"]]}',
			)
		)
		self.assertEqual(payload["value"], 2)


class TestABlankGroupIsNamedNotFiltered(ExecutorCase):
	def test_a_blank_group_reads_as_not_set(self):
		payload = executor.run(_chart(group_by_field="assigned_to"))
		labels = {point["label"] for point in payload["points"]}
		self.assertIn("Not set", labels)

	def test_a_blank_group_drills_on_the_blank_it_really_had(self):
		"""The words the reader saw must never reach the filter — they are not a value anything holds."""
		payload = executor.run(_chart(group_by_field="assigned_to"))
		blank = next(point for point in payload["points"] if point["label"] == "Not set")
		self.assertEqual(blank["drill"]["filters"]["assigned_to"], ["is", "not set"])

	def test_the_blank_slice_counts_every_blank_row(self):
		payload = executor.run(_chart(group_by_field="assigned_to"))
		blank = next(point for point in payload["points"] if point["label"] == "Not set")
		self.assertEqual(blank["value"], 5)


class TestTheLabelIsDisplayAndTheGroupIsTheFilter(ExecutorCase):
	def test_a_label_column_may_reach_through_a_link(self):
		payload = executor.run(_chart(group_by_field="assigned_to", label_field="assigned_to.full_name"))
		named = next(point for point in payload["points"] if point["raw"] == PROBE_USER)
		self.assertEqual(named["label"], "Exec Probe")

	def test_the_drill_carries_the_raw_value_and_not_the_label(self):
		payload = executor.run(_chart(group_by_field="assigned_to", label_field="assigned_to.full_name"))
		named = next(point for point in payload["points"] if point["raw"] == PROBE_USER)
		self.assertEqual(named["drill"]["filters"]["assigned_to"], PROBE_USER)


class TestDrillIsADeclaredChoice(ExecutorCase):
	def test_a_drillable_card_names_the_list_it_opens(self):
		payload = executor.run(_chart())
		self.assertEqual(payload["drill"]["doctype"], TASK)
		self.assertEqual(payload["drill"]["route"], "Tasks")

	def test_a_card_that_is_not_drillable_carries_no_drill_at_all(self):
		payload = executor.run(_chart(drill_enabled=0))
		self.assertNotIn("drill", payload)
		self.assertTrue(all("drill" not in point for point in payload["points"]))

	def test_the_drill_filter_carries_the_cards_own_narrowing(self):
		"""Base filters and the point's own group together, or the list shows rows the card never counted."""
		payload = executor.run(_chart())
		point = self._by_raw(payload)["Done"]
		self.assertEqual(point["drill"]["filters"]["status"], "Done")
		self.assertEqual(point["drill"]["filters"]["title"], ["like", f"{PROBE}%"])
