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

A card may also declare a SECOND dimension and a time bucket, and each carries one more assumption the
executor makes and cannot check on a rep's screen:

  * THE SPLIT IS A REAL, LOCAL COLUMN, for the same reason the grouped one is — a click on a cell filters
    on both dimensions, and only a column of that list can be filtered.
  * BOTH DIMENSIONS COME OUT OF ONE QUERY, so neither side of a cross may be a DERIVED field: a derived
    field is no column at all and is counted a bucket at a time. Grouped by one, never crossed with one.
  * A MONTH BUCKET HAS A DATE TO TAKE THE MONTH OF, and is an axis in its own right — so a card that
    declares one needs nothing in Group By Field.
  * A NUMBER CARD HAS NEITHER. It is one figure; a second dimension and a time bucket are both breakdowns.
  * A DISTINCT COUNT IS A SINGLE FIGURE, counted as a number of groups, so it has no breakdown to draw.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.dashboard.test_chart_declaration
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.dashboard import declaration
from tatva_connect.list_engine import derived

CHART = "CRM Dashboard Chart"
PROBE = "probe_chart"
LEAD = "CRM Lead"


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

	def test_a_card_that_declares_neither_reads_as_it_did_before_either_field_existed(self):
		"""Catches a default that quietly turns every shipped card into a two-dimension one."""
		doc = _chart().insert(ignore_permissions=True)
		self.assertEqual(doc.split_by, "")
		self.assertEqual(doc.time_bucket, declaration.NO_BUCKET)


class TestASecondDimensionIsProvedToo(ChartDeclarationCase):
	"""The split, the bucket and the distinct count, each refused at Save for what the executor assumes.

	A derived field is registered here rather than leaned on, so these hold on a site whose operator has
	authored none — and on `CRM Lead`, which declares none of its own, so nothing shipped is disturbed."""

	DERIVED = "_probe_chart_lead_state"

	def setUp(self):
		super().setUp()
		derived.register(
			derived.DerivedField(
				doctype=LEAD,
				fieldname=self.DERIVED,
				label="Probe Lead State",
				buckets=[
					derived.Bucket("Converted", [("converted", "=", 1)]),
					derived.Bucket("Open", [("converted", "=", 0)]),
				],
			)
		)
		self.addCleanup(derived._REGISTRY.get(LEAD, {}).pop, self.DERIVED, None)
		self.addCleanup(derived.reload)

	def test_a_split_column_the_list_does_not_have_is_refused_by_name(self):
		"""Catches a split that reaches SQL as an unknown column and kills the card on a rep's screen."""
		message = self._refused(chart_type="stacked_bar", split_by="no_such_column")
		self.assertIn("no_such_column", message)

	def test_a_split_column_reaching_through_a_link_is_refused_by_name(self):
		"""Catches a cell drill filtering on a column of another record, which returns nothing."""
		message = self._refused(chart_type="stacked_bar", split_by="lead_owner.full_name")
		self.assertIn("lead_owner.full_name", message)

	def test_splitting_by_the_column_it_is_already_grouped_by_is_refused(self):
		"""Catches one dimension counted twice, which draws a diagonal and calls it a breakdown."""
		self.assertTrue(self._refused(chart_type="stacked_bar", group_by_field="source", split_by="source"))

	def test_a_heatmap_with_nothing_to_cross_is_refused(self):
		"""Catches a heatmap with one dimension, which renders as a single row of cells."""
		self.assertTrue(self._refused(chart_type="heatmap", group_by_field="source", split_by=""))

	def test_a_sound_split_saves(self):
		doc = _chart(chart_type="stacked_bar", group_by_field="source", split_by="lead_owner").insert(
			ignore_permissions=True
		)
		self.assertEqual(doc.split_by, "lead_owner")

	def test_a_number_card_may_not_carry_a_split(self):
		"""Catches a second dimension declared where nothing is drawn to hold it."""
		self.assertTrue(self._refused(chart_type="number", group_by_field="", split_by="source"))

	def test_a_number_card_may_not_carry_a_time_bucket(self):
		self.assertTrue(
			self._refused(chart_type="number", group_by_field="", time_bucket=declaration.MONTH)
		)

	def test_a_month_bucket_with_no_date_column_is_refused(self):
		"""Catches `MONTH(NULL)`: the axis the card is drawn along would not exist."""
		self.assertTrue(
			self._refused(chart_type="line", group_by_field="", date_field="", time_bucket=declaration.MONTH)
		)

	def test_a_month_bucket_is_an_axis_in_its_own_right(self):
		"""A trend needs no Group By Field; refusing it here would make a line chart undeclarable."""
		doc = _chart(
			chart_type="line", group_by_field="", date_field="creation", time_bucket=declaration.MONTH
		).insert(ignore_permissions=True)
		self.assertEqual(doc.time_bucket, declaration.MONTH)

	def test_a_grouped_card_with_neither_an_axis_nor_a_bucket_is_refused(self):
		self.assertTrue(self._refused(chart_type="line", group_by_field="", time_bucket=declaration.NO_BUCKET))

	def test_a_card_may_be_grouped_by_a_derived_field(self):
		"""Catches the column check being applied to a field that is deliberately not a column."""
		doc = _chart(chart_type="donut", group_by_field=self.DERIVED).insert(ignore_permissions=True)
		self.assertEqual(doc.group_by_field, self.DERIVED)

	def test_a_card_split_by_a_derived_field_is_refused_by_name(self):
		"""Catches a cross whose second side cannot be named in a group-by at all."""
		message = self._refused(chart_type="stacked_bar", group_by_field="source", split_by=self.DERIVED)
		self.assertIn(self.DERIVED, message)

	def test_a_card_grouped_by_a_derived_field_may_not_also_be_crossed(self):
		"""Catches a split silently dropped: a derived group is counted per bucket and never grouped."""
		message = self._refused(
			chart_type="stacked_bar", group_by_field=self.DERIVED, split_by="lead_owner"
		)
		self.assertIn(self.DERIVED, message)

	def test_a_distinct_count_over_nothing_is_refused(self):
		self.assertTrue(
			self._refused(chart_type="number", group_by_field="", aggregate=declaration.DISTINCT, aggregate_field="")
		)

	def test_a_distinct_count_on_a_card_that_breaks_down_is_refused(self):
		"""Catches `DISTINCT` reaching `fields=` as an aggregate function, which frappe has no such thing for."""
		self.assertTrue(
			self._refused(
				chart_type="donut",
				group_by_field="source",
				aggregate=declaration.DISTINCT,
				aggregate_field="lead_owner",
			)
		)

	def test_a_distinct_number_card_saves(self):
		doc = _chart(
			chart_type="number",
			group_by_field="",
			aggregate=declaration.DISTINCT,
			aggregate_field="lead_owner",
		).insert(ignore_permissions=True)
		self.assertEqual(doc.aggregate, declaration.DISTINCT)
