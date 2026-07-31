# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A layout POINTS AT cards; it never carries one.

That is the rule the whole shape rests on (CLAUDE.md -> I5). A layout that inlined a card definition would
be a second copy of it, and the day somebody corrected the card the layouts would keep showing the old
one — silently, because nothing would go red. So the only thing a layout says about a card is its name,
and a name at nothing is refused at Save, where an operator is present to read why.

The rest is arithmetic that has to hold before anyone can draw with it: a list of placements, each
complete, each card once.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.dashboard.test_layout_declaration
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.dashboard import executor

CHART = "CRM Dashboard Chart"
LAYOUT = "CRM Dashboard Layout"
PROBE_CHART = "probe_layout_card"
PROBE_ROLE = "Probe Dashboard Role"


def _placed(chart=PROBE_CHART, **overrides):
	placement = {"chart": chart, "x": 0, "y": 0, "w": 4, "h": 3}
	placement.update(overrides)
	return placement


class LayoutDeclarationCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._clear()
		if not frappe.db.exists("Role", PROBE_ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": PROBE_ROLE, "desk_access": 0}).insert(
				ignore_permissions=True
			)
		frappe.get_doc(
			{
				"doctype": CHART,
				"chart_name": PROBE_CHART,
				"label": "Probe Card",
				"chart_type": "number",
				"source_doctype": "CRM Lead",
				"aggregate": "COUNT",
				"date_field": "creation",
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		self._clear()

	def _clear(self):
		frappe.db.delete(LAYOUT, {"role": PROBE_ROLE})
		frappe.db.delete(CHART, {"chart_name": ["like", "probe_layout%"]})
		frappe.db.delete("Role", {"name": PROBE_ROLE})

	def _layout(self, **overrides):
		row = {
			"doctype": LAYOUT,
			"role": PROBE_ROLE,
			"title": "Probe Dashboard",
			"enabled": 1,
			"priority": 0,
			"layout": json.dumps([_placed()]),
			"exposed_filters": json.dumps(["date_range"]),
		}
		row.update(overrides)
		return frappe.get_doc(row)

	def _refused(self, **overrides):
		with self.assertRaises(frappe.ValidationError) as refusal:
			self._layout(**overrides).insert(ignore_permissions=True)
		return str(refusal.exception)


class TestALayoutOnlyPointsAtCardsThatExist(LayoutDeclarationCase):
	def test_a_sound_layout_saves(self):
		doc = self._layout().insert(ignore_permissions=True)
		self.assertEqual(doc.name, PROBE_ROLE)

	def test_a_card_that_does_not_exist_is_refused_by_name(self):
		"""The pointer IS the relationship, so a pointer at nothing is the one way this shape breaks."""
		message = self._refused(layout=json.dumps([_placed(chart="no_such_card")]))
		self.assertIn("no_such_card", message)

	def test_a_layout_that_is_not_a_list_is_refused(self):
		self.assertTrue(self._refused(layout=json.dumps({"chart": PROBE_CHART})))

	def test_a_layout_that_is_not_json_is_refused(self):
		self.assertTrue(self._refused(layout="chart: total_leads"))

	def test_a_placement_missing_its_position_is_refused(self):
		message = self._refused(layout=json.dumps([{"chart": PROBE_CHART, "x": 0, "y": 0}]))
		self.assertIn("w", message)

	def test_the_same_card_twice_is_refused_by_name(self):
		message = self._refused(layout=json.dumps([_placed(), _placed(x=4)]))
		self.assertIn(PROBE_CHART, message)


class TestExposedFiltersAreOnesThatDoSomething(LayoutDeclarationCase):
	def test_a_filter_nothing_applies_is_refused_by_name(self):
		message = self._refused(exposed_filters=json.dumps(["date_range", "moon_phase"]))
		self.assertIn("moon_phase", message)

	def test_exposed_filters_that_are_not_a_list_are_refused(self):
		self.assertTrue(self._refused(exposed_filters=json.dumps({"date_range": True})))

	def test_every_known_filter_is_accepted(self):
		doc = self._layout(exposed_filters=json.dumps(list(executor.KNOWN_FILTERS))).insert(
			ignore_permissions=True
		)
		self.assertEqual(frappe.parse_json(doc.exposed_filters), list(executor.KNOWN_FILTERS))

