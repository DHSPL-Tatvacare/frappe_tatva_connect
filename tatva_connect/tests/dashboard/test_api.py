# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The endpoint answers for WHOEVER IS ASKING, and it cannot be asked for anything else.

The caller sends a date range and the filters their own layout offered them. It cannot name a card, a
list or a column, so there is nothing to guess at: what is shown is resolved server-side from the caller's
own roles, and there is no endpoint that takes a card name.

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

from tatva_connect.dashboard import api, declaration

CHART = declaration.CHART
LAYOUT = declaration.LAYOUT
PROBE_ROLE = "Probe Dash Api"
PROBE_TITLE = "Probe Dashboard"
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
		# db.delete does not cascade to child rows, and `field:title` remints the same parent name — so
		# without this the next dashboard inherits the last one's placements.
		frappe.db.delete(declaration.PLACEMENT_DOCTYPE, {"parenttype": LAYOUT, "parent": PROBE_TITLE})
		frappe.db.delete(LAYOUT, {"role": PROBE_ROLE})
		frappe.db.delete("Has Role", {"role": PROBE_ROLE})
		frappe.db.delete("User", {"name": PROBE_USER})
		frappe.db.delete("Role", {"name": PROBE_ROLE})
		frappe.db.delete(CHART, {"chart_name": ["like", "probe_api%"]})
		frappe.db.delete("CRM Lead", {"first_name": ["like", "ApiProbe%"]})
		declaration.retire_cache()
		self._forget()

	def _forget(self):
		setattr(frappe.local, "tatva_connect:dashboard_layout", {})

	def _seed_layout(self, *charts):
		frappe.get_doc(
			{
				"doctype": LAYOUT,
				"role": PROBE_ROLE,
				"title": PROBE_TITLE,
				"enabled": 1,
				"priority": 500,
				"charts": [{"chart": name, "x": i * 2, "y": 0, "w": 2, "h": 2} for i, name in enumerate(charts)],
				"exposed_filters": json.dumps(["date_range"]),
			}
		).insert(ignore_permissions=True)
		self._forget()

	def _as_probe(self):
		frappe.set_user(PROBE_USER)
		self._forget()


class TestAnUnconfiguredRoleIsAnAnswer(ApiCase):
	def test_a_user_with_no_layout_is_told_so_without_an_error(self):
		"""The probe holds ONLY its own role here. setUp lends it Sales User for the read permission the
		other cases need, but that role now carries a seeded rep board — and a user who matches ANY layout
		is not the subject of this test. No permission is lent back: the endpoint answers off the resolver
		and returns before it reads a single chart."""
		frappe.db.delete("Has Role", {"parent": PROBE_USER, "role": "Sales User"})
		frappe.clear_cache(user=PROBE_USER)
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
		# The payload is what the page DRAWS: name, wording and the column it narrows — decided server-side.
		self.assertEqual(
			api.get_dashboard()["filters"],
			[{"name": "date_range", "label": "Period", "column": None}],
		)


class TestOneCardCannotTakeTheDashboardDown(ApiCase):
	def test_a_broken_declaration_costs_its_own_card_only(self):
		self._seed_layout(GOOD, BROKEN)
		self._as_probe()
		cards = {card["chart"]: card for card in api.get_dashboard()["charts"]}
		self.assertNotIn("error", cards[GOOD])
		self.assertIn("error", cards[BROKEN])



class TestACachedDashboardIsOnePersonsAnswer(ApiCase):
	"""The figures are cached, and the row gate differs per person — so the cache key has to carry the user
	or one rep's dashboard is served to another. That is the failure this class exists for."""

	SECOND_USER = "probe-api-two@tatvacare.test"

	def _lead_for(self, owner):
		frappe.get_doc(
			{
				"doctype": "CRM Lead",
				"first_name": f"ApiProbe {owner}",
				"status": frappe.db.get_value("CRM Lead Status", {}, "name"),
				"lead_owner": owner,
			}
		).insert(ignore_permissions=True)

	def _second_user(self):
		frappe.db.delete("Has Role", {"parent": self.SECOND_USER})
		frappe.db.delete("User", {"name": self.SECOND_USER})
		user = frappe.get_doc(
			{"doctype": "User", "email": self.SECOND_USER, "first_name": "Two", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		user.add_roles(PROBE_ROLE, "Sales User")
		return user.name

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.delete("Has Role", {"parent": self.SECOND_USER})
		frappe.db.delete("User", {"name": self.SECOND_USER})
		super().tearDown()

	def test_a_second_ask_is_answered_from_the_cache(self):
		self._seed_layout(GOOD)
		self._as_probe()
		self.assertEqual(api.get_dashboard()["charts"][0]["value"], 0)
		frappe.set_user("Administrator")
		self._lead_for(PROBE_USER)
		self._as_probe()
		self.assertEqual(
			api.get_dashboard()["charts"][0]["value"], 0, "the second ask re-counted instead of reading the cache"
		)

	def test_retiring_the_cache_shows_the_new_figure(self):
		self._seed_layout(GOOD)
		self._as_probe()
		api.get_dashboard()
		frappe.set_user("Administrator")
		self._lead_for(PROBE_USER)
		declaration.retire_cache()
		self._as_probe()
		self.assertEqual(api.get_dashboard()["charts"][0]["value"], 1)

	def test_one_users_dashboard_is_never_served_to_another(self):
		"""The leak: two people on the same layout, one owning a lead the other cannot see."""
		second = self._second_user()
		self._seed_layout(GOOD)
		self._lead_for(PROBE_USER)
		self._as_probe()
		self.assertEqual(api.get_dashboard()["charts"][0]["value"], 1)
		frappe.set_user(second)
		self._forget()
		self.assertEqual(
			api.get_dashboard()["charts"][0]["value"], 0, "a cached dashboard leaked across users"
		)

	def test_saving_a_card_retires_every_cached_dashboard(self):
		self._seed_layout(GOOD)
		self._as_probe()
		api.get_dashboard()
		frappe.set_user("Administrator")
		self._lead_for(PROBE_USER)
		frappe.get_doc(CHART, GOOD).save(ignore_permissions=True)
		self._as_probe()
		self.assertEqual(api.get_dashboard()["charts"][0]["value"], 1)
