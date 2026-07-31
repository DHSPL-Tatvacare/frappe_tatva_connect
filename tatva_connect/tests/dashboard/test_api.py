# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The endpoint answers for WHOEVER IS ASKING, and it cannot be asked for anything else.

The caller sends a date range and the filters their own layout offered them. It cannot name a card, a
list or a column, so there is nothing to guess at — and `get_chart`, which does take a name, refuses any
name the caller's own layout does not already place. Without that refusal a single-card refresh would be
a way to read any card on the site by guessing its slug.

Two more properties matter as much as the answer itself:

  * NOT CONFIGURED IS A NORMAL ANSWER. A role nobody has set up yet gets `configured: false` and a 200.
    Raising would put an error in front of somebody whose only problem is that an operator has not chosen
    their cards.
  * ONE BAD CARD COSTS ONE CARD. A declaration that throws is caught and returned with an `error` key in
    its place; the other nine still answer. A dashboard that dies whole because of one bad row is a
    dashboard nobody can fix while it is broken.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.dashboard.test_api
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.dashboard import api

CHART = "CRM Dashboard Chart"
LAYOUT = "CRM Dashboard Layout"
PROBE_ROLE = "Probe Dash Api"
PROBE_USER = "probe-api@tatvacare.test"
STRANGER = "probe_api_unplaced"
GOOD = "probe_api_good"
BROKEN = "probe_api_broken"


def _chart(chart_name, **overrides):
	row = {
		"doctype": CHART,
		"chart_name": chart_name,
		"label": chart_name,
		"chart_type": "number",
		# CRM Lead, because its gate has no off switch — a task card on a plain role is refused at Save, and
		# this suite is about the endpoint, not about that refusal (test_layout_declaration covers it).
		"source_doctype": "CRM Lead",
		"aggregate": "COUNT",
		"date_field": "creation",
		"honours_date_range": 0,
		"base_filters": "{}",
	}
	row.update(overrides)
	return row


class ApiCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._clear()
		if not frappe.db.exists("Role", PROBE_ROLE):
			frappe.get_doc({"doctype": "Role", "role_name": PROBE_ROLE, "desk_access": 0}).insert(
				ignore_permissions=True
			)
		user = frappe.get_doc(
			{"doctype": "User", "email": PROBE_USER, "first_name": "Probe", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		# Sales User carries the read permission every chart needs; the probe role carries the layout.
		user.add_roles(PROBE_ROLE, "Sales User")
		for row in (
			_chart(GOOD),
			_chart(STRANGER),
			# Passes every save-time rule and still cannot run: base filters are not checked against columns.
			_chart(BROKEN, base_filters='{"no_such_column": "x"}'),
		):
			frappe.get_doc(row).insert(ignore_permissions=True)

	def tearDown(self):
		self._clear()
		frappe.set_user("Administrator")

	def _clear(self):
		frappe.db.delete(LAYOUT, {"role": PROBE_ROLE})
		frappe.db.delete("Has Role", {"role": PROBE_ROLE})
		frappe.db.delete("User", {"name": PROBE_USER})
		frappe.db.delete("Role", {"name": PROBE_ROLE})
		frappe.db.delete(CHART, {"chart_name": ["like", "probe_api%"]})
		self._forget()

	def _forget(self):
		setattr(frappe.local, "tatva_connect:dashboard_layout", {})

	def _seed_layout(self, *charts):
		frappe.get_doc(
			{
				"doctype": LAYOUT,
				"role": PROBE_ROLE,
				"title": "Probe Dashboard",
				"enabled": 1,
				"priority": 500,
				"layout": json.dumps(
					[{"chart": name, "x": i * 2, "y": 0, "w": 2, "h": 2} for i, name in enumerate(charts)]
				),
				"exposed_filters": json.dumps(["date_range"]),
			}
		).insert(ignore_permissions=True)
		self._forget()

	def _as_probe(self):
		frappe.set_user(PROBE_USER)
		self._forget()


class TestAnUnconfiguredRoleIsAnAnswer(ApiCase):
	def test_a_user_with_no_layout_is_told_so_without_an_error(self):
		self._as_probe()
		payload = api.get_dashboard()
		self.assertFalse(payload["configured"])
		self.assertEqual(payload["charts"], [])
		self.assertEqual(payload["filters"], [])


class TestTheDashboardIsTheCallersOwn(ApiCase):
	def test_a_user_gets_the_cards_their_layout_places(self):
		self._seed_layout(GOOD)
		self._as_probe()
		payload = api.get_dashboard()
		self.assertTrue(payload["configured"])
		self.assertEqual([card["chart"] for card in payload["charts"]], [GOOD])

	def test_a_card_no_layout_places_is_not_in_the_answer(self):
		self._seed_layout(GOOD)
		self._as_probe()
		payload = api.get_dashboard()
		self.assertNotIn(STRANGER, [card["chart"] for card in payload["charts"]])

	def test_every_card_carries_where_it_sits(self):
		self._seed_layout(GOOD)
		self._as_probe()
		card = api.get_dashboard()["charts"][0]
		self.assertEqual((card["x"], card["y"], card["w"], card["h"]), (0, 0, 2, 2))

	def test_the_exposed_filters_are_the_layouts_own(self):
		self._seed_layout(GOOD)
		self._as_probe()
		self.assertEqual(api.get_dashboard()["filters"], ["date_range"])


class TestOneCardCannotTakeTheDashboardDown(ApiCase):
	def test_a_broken_declaration_costs_its_own_card_only(self):
		self._seed_layout(GOOD, BROKEN)
		self._as_probe()
		cards = {card["chart"]: card for card in api.get_dashboard()["charts"]}
		self.assertNotIn("error", cards[GOOD])
		self.assertIn("error", cards[BROKEN])


class TestASingleCardRefreshIsNotAWayToReadAnyCard(ApiCase):
	def test_a_card_the_layout_places_refreshes(self):
		self._seed_layout(GOOD)
		self._as_probe()
		self.assertEqual(api.get_chart(GOOD)["chart"], GOOD)

	def test_a_card_outside_the_callers_layout_is_refused(self):
		self._seed_layout(GOOD)
		self._as_probe()
		with self.assertRaises(frappe.PermissionError):
			api.get_chart(STRANGER)

	def test_a_card_that_does_not_exist_is_refused_the_same_way(self):
		"""Missing and not-yours answer alike, so guessing a slug never confirms one exists."""
		self._seed_layout(GOOD)
		self._as_probe()
		with self.assertRaises(frappe.PermissionError):
			api.get_chart("no_such_card_at_all")
