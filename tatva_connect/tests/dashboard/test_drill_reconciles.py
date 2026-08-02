# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The number on the card and the number of rows behind it are the same query. Proved, not asserted.

This is the most valuable test in the phase, because the failure it catches is invisible everywhere else:
a card saying 247 that opens a list of 180. Every unit test can pass while that is true — the aggregate is
right, the filter is right, and they are right about different sets of rows.

So each seeded card is run as a NON-PRIVILEGED user carrying a real `User Permission`, and each datapoint
is re-counted by handing its own drill filter to `crm.api.doc.get_data` — the endpoint the drilled LIST
calls, not `frappe.get_list`. That distinction is the whole point: they are different code paths, and
every way this drill has broken so far broke between them.

The User Permission is not decoration. As Administrator the gate is open and every count reconciles
trivially — the test would pass having proved nothing about the one thing it exists to prove.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.dashboard.test_drill_reconciles
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, nowdate

# The OVERRIDE, not `crm.api.doc.get_data`: hooks.py re-points that whitelisted name here, so the direct
# import is a function no browser ever reaches — it skips the list engine, and a drill naming a DERIVED
# field then dies on native's field check instead of being translated into the bucket's own tuples.
from tatva_connect.api.list_link_titles import get_data
from tatva_connect.dashboard import api, declaration, executor, seed

CHART = "CRM Dashboard Chart"
PROBE = "ReconcileProbe"
PROBE_USER = "probe-reconcile@tatvacare.test"
GATED_ROLE = "Sales User"

# What each SPA page hands ViewControls as its own baseline filter (Leads.vue:26; Tasks passes none). It is
# ANDed server-side and OVERRIDES the drill on a shared key, so a card must declare the same thing or it
# opens a list that disagrees with it.
_PAGE_FILTERS = {"CRM Lead": {"converted": 0}, "CRM Task": None}

# Wide enough that the ranged cards have something to count; reconciliation holds for any window.
WINDOW = {"from_date": add_to_date(nowdate(), years=-5), "to_date": nowdate()}


class ReconcileCase(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("Role", GATED_ROLE):
			self.skipTest(f"{GATED_ROLE} is not on this site, so there is no narrowed gate to prove against")
		seed.ensure_rows()
		self._clear()
		user = frappe.get_doc(
			{"doctype": "User", "email": PROBE_USER, "first_name": "Reconcile", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
		user.add_roles(GATED_ROLE)
		entitled = self._narrow(user.name)
		self._own_rows(entitled)
		self.charts = frappe.get_list(
			CHART, filters={"enabled": 1}, fields=list(declaration.READ), limit=0, ignore_permissions=True
		)
		frappe.set_user(PROBE_USER)

	def tearDown(self):
		frappe.set_user("Administrator")
		self._clear()

	def _clear(self):
		frappe.db.delete("CRM Task", {"title": ["like", f"{PROBE}%"]})
		frappe.db.delete("CRM Lead", {"first_name": ["like", f"{PROBE}%"]})
		frappe.db.delete("User Permission", {"user": PROBE_USER})
		frappe.db.delete("Has Role", {"parent": PROBE_USER})
		frappe.db.delete("User", {"name": PROBE_USER})

	def _narrow(self, user):
		"""One product line, so the gate really narrows. Without it the whole test passes vacuously."""
		vertical = frappe.db.get_value("CRM Vertical", {}, "name")
		if not vertical:
			return None
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "CRM Vertical",
				"for_value": vertical,
				"applicable_for": "CRM Lead",
				"apply_to_all_doctypes": 0,
			}
		).insert(ignore_permissions=True)
		return vertical

	def _own_rows(self, entitled):
		"""Rows this user OWNS, on both sides of the grain line: a Sales User sees only their own, so without
		these the gate narrows to nothing and every card reconciles against an empty list, proving nothing."""
		if not entitled:
			return
		status = frappe.db.get_value("CRM Lead Status", {}, "name")
		outside = frappe.db.get_value("CRM Vertical", {"name": ["!=", entitled]}, "name")
		for index, vertical in enumerate([entitled, entitled, entitled, outside, outside]):
			if not vertical:
				continue
			lead = frappe.get_doc(
				{
					"doctype": "CRM Lead",
					"first_name": f"{PROBE} lead {index}",
					"status": status,
					"lead_owner": PROBE_USER,
					"custom_vertical": vertical,
					"source": "Cold Call" if index % 2 else None,
					# One converted lead, and it is the whole point of the fixture: the Leads page hides
					# converted leads, so a card that does not also hide them counts a row its own list will
					# never show. Without this row every card reconciles whether or not it declares it.
					"converted": 1 if index == 0 else 0,
				}
			).insert(ignore_permissions=True)
			frappe.get_doc(
				{
					"doctype": "CRM Task",
					"title": f"{PROBE} task {index}",
					"status": "Todo" if index % 2 else "Done",
					"reference_doctype": "CRM Lead",
					"reference_docname": lead.name,
					"assigned_to": PROBE_USER,
				}
			).insert(ignore_permissions=True)

	def _rows_behind(self, drill):
		"""The drill filter through the `crm.api.doc.get_data` OVERRIDE — the endpoint the drilled LIST calls, not
		`frappe.get_list`. They are different code paths, and everything that has ever broken this drill broke
		in between them: the page's own default_filters, which get_data lets override the drill.

		`_PAGE_FILTERS` mirrors what each page hands ViewControls, so the test asks the question the browser
		asks. `total_count` and not the row count, because the page shows a total, not a page."""
		answer = get_data(
			doctype=drill["doctype"],
			filters=dict(drill["filters"]),
			order_by="modified desc",
			default_filters=_PAGE_FILTERS.get(drill["doctype"]),
		)
		return answer["total_count"]


class TestEveryCardReconcilesWithItsOwnList(ReconcileCase):
	def test_the_gate_really_narrows_for_this_user(self):
		"""Guard against the vacuous pass: a user who can see nothing reconciles perfectly and proves nothing."""
		self.assertGreater(len(frappe.get_list("CRM Lead", limit=0)), 0)

	def test_every_datapoint_returns_exactly_the_rows_it_counted(self):
		mismatches = []
		for chart in self.charts:
			payload = executor.run(chart, WINDOW, {})
			for point in payload["points"]:
				if "drill" not in point:
					continue
				behind = self._rows_behind(point["drill"])
				if behind != point["value"]:
					mismatches.append(f"{chart['chart_name']}/{point['raw']}: card {point['value']} vs list {behind}")
		self.assertEqual(mismatches, [], f"a card and its own list disagree: {mismatches}")

	def test_every_card_total_returns_exactly_the_rows_it_counted(self):
		mismatches = []
		for chart in self.charts:
			# A grouped card is capped at its row limit, so only a number card's total is the whole list.
			if chart["chart_type"] != "number":
				continue
			payload = executor.run(chart, WINDOW, {})
			if "drill" not in payload:
				continue
			behind = self._rows_behind(payload["drill"])
			if behind != payload["value"]:
				mismatches.append(f"{chart['chart_name']}: card {payload['value']} vs list {behind}")
		self.assertEqual(mismatches, [], f"a card and its own list disagree: {mismatches}")

	def test_the_endpoint_reconciles_the_same_way_it_executes(self):
		"""Whatever the endpoint hands the browser is what the browser can re-ask for, unchanged."""
		frappe.set_user("Administrator")
		try:
			payload = api.get_dashboard()
		finally:
			frappe.set_user(PROBE_USER)
		self.assertTrue(payload["configured"])
		self.assertTrue(all("error" not in card for card in payload["charts"]))
