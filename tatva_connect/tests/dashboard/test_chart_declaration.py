# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A card that cannot be run must fail the DAY IT IS DECLARED, not on somebody's dashboard.

Every rule the executor RELIES ON is asserted at Save, because the alternative is a dead tile among nine
live ones — the kind of failure nobody reports and everybody works around. Six things are proved here,
and each maps to exactly one thing `executor.run` assumes:

  * THE LIST IS ONE THE EXECUTOR RUNS. Two lists, declared as a Select, so a card can never be pointed at
    a list nobody checked it against.
  * THE SHAPE MATCHES THE TYPE. A number card has nothing to break down; a donut with nothing to break
    down is a single slice pretending to be a distribution.
  * THE GROUPED COLUMN IS A REAL, LOCAL COLUMN. This is the load-bearing one: the same column names the
    slice AND filters the drill-down, and a column reached through a link can be shown but not filtered
    on. A dot here is the silent way the card and the list stop agreeing.
  * THE DATE COLUMN EXISTS — including the framework's own, so `creation` is accepted.
  * SUM AND AVG SAY WHAT THEY MEASURE.
  * BASE FILTERS ARE AN OBJECT, because what is typed there is handed to `frappe.get_list` unchanged.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.dashboard.test_chart_declaration
"""

import frappe
from frappe.tests.utils import FrappeTestCase

CHART = "CRM Dashboard Chart"
PROBE = "probe_chart"


def _chart(**overrides):
	"""A minimal sound declaration, so each test varies exactly the one thing it is about."""
	row = {
		"doctype": CHART,
		"chart_name": PROBE,
		"label": "Probe Card",
		"chart_type": "donut",
		"source_doctype": "CRM Lead",
		"aggregate": "COUNT",
		"group_by_field": "source",
		"date_field": "creation",
		"honours_date_range": 1,
		"base_filters": "{}",
		"row_limit": 10,
		"drill_enabled": 1,
	}
	row.update(overrides)
	return frappe.get_doc(row)


class ChartDeclarationCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete(CHART, {"chart_name": ["like", f"{PROBE}%"]})

	def tearDown(self):
		frappe.db.delete(CHART, {"chart_name": ["like", f"{PROBE}%"]})

	def _refused(self, **overrides):
		with self.assertRaises(frappe.ValidationError) as refusal:
			_chart(**overrides).insert(ignore_permissions=True)
		return str(refusal.exception)


class TestAChartIsProvedBeforeItExists(ChartDeclarationCase):
	def test_a_sound_declaration_saves(self):
		doc = _chart().insert(ignore_permissions=True)
		self.assertEqual(doc.name, PROBE)

	def test_a_list_the_executor_cannot_run_is_refused(self):
		self.assertTrue(self._refused(source_doctype="CRM Deal"))

	def test_a_number_card_may_not_be_grouped(self):
		message = self._refused(chart_type="number", group_by_field="source")
		self.assertIn("source", message)

	def test_a_donut_with_nothing_to_break_down_is_refused(self):
		self.assertTrue(self._refused(chart_type="donut", group_by_field=""))

	def test_a_grouped_column_reaching_through_a_link_is_refused_by_name(self):
		"""The drill-down filters on the grouped column, and a column of another record cannot be filtered."""
		message = self._refused(group_by_field="lead_owner.full_name")
		self.assertIn("lead_owner.full_name", message)

	def test_a_grouped_column_the_list_does_not_have_is_refused_by_name(self):
		message = self._refused(group_by_field="no_such_column")
		self.assertIn("no_such_column", message)

	def test_a_label_column_may_reach_through_a_link(self):
		"""Traversal is display only, and display is the one place it is safe."""
		doc = _chart(group_by_field="lead_owner", label_field="lead_owner.full_name").insert(
			ignore_permissions=True
		)
		self.assertEqual(doc.label_field, "lead_owner.full_name")

	def test_a_date_column_the_list_does_not_have_is_refused(self):
		self.assertTrue(self._refused(date_field="no_such_date"))

	def test_the_frameworks_own_columns_count_as_columns(self):
		"""`creation` is not in the meta's field list and is still a real column — the seed dates by it."""
		doc = _chart(date_field="creation").insert(ignore_permissions=True)
		self.assertEqual(doc.date_field, "creation")

	def test_an_aggregate_over_nothing_is_refused(self):
		self.assertTrue(self._refused(aggregate="SUM", aggregate_field=""))

	def test_an_aggregate_over_a_column_the_list_does_not_have_is_refused(self):
		self.assertTrue(self._refused(aggregate="AVG", aggregate_field="no_such_number"))

	def test_base_filters_that_are_not_an_object_are_refused(self):
		self.assertTrue(self._refused(base_filters='["status", "=", "Done"]'))

	def test_base_filters_that_are_not_json_are_refused(self):
		self.assertTrue(self._refused(base_filters="status = Done"))

	def test_base_filters_are_handed_to_get_list_unchanged(self):
		doc = _chart(base_filters='{"status": ["in", ["Open"]]}').insert(ignore_permissions=True)
		self.assertEqual(frappe.parse_json(doc.base_filters), {"status": ["in", ["Open"]]})
