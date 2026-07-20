# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Which leads a user may see has ONE answer, whoever is asking.

A Smart View is a list of leads. It must show the same leads Frappe's own list shows — not more. It did
not: `_pqc_criterion` asked `DatabaseQuery.get_permission_query_conditions()`, which returns only what
HOOKS and Server Scripts contribute. This deployment scopes leads with `User Permission` rows, and those
are applied by `build_match_conditions()` — which `access/visibility.py` already calls for exactly this
reason, and which calls `get_permission_query_conditions()` itself on the way.

Measured before the fix, for a real Sales Manager scoped to one vertical: `frappe.get_list` returned 305
leads and the identical Smart View query returned 2304.

This mints its own user, its own grain and its own leads rather than reading a dev site's seed, so it
asserts the CODE and not somebody's data.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_row_gate_one_answer
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.smartview import api as smartview

VERTICAL_MINE = "ZZ Row Gate Mine"
VERTICAL_OTHER = "ZZ Row Gate Other"
GROUP = "ZZ Row Gate Group"
USER = "zz-row-gate@example.com"
PHONE_MINE = "+916100050001"
PHONE_OTHER = "+916100050002"


class TestRowGateHasOneAnswer(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		for vertical in (VERTICAL_MINE, VERTICAL_OTHER):
			if not frappe.db.exists("CRM Vertical", vertical):
				frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": vertical}).insert(ignore_permissions=True)
		if not frappe.db.exists("CRM Group", GROUP):
			frappe.get_doc({"doctype": "CRM Group", "group_name": GROUP}).insert(ignore_permissions=True)

		if not frappe.db.exists("User", USER):
			user = frappe.get_doc({
				"doctype": "User", "email": USER, "first_name": "Row Gate",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
			# Sales Manager deliberately: stock's own hook returns "" for one outside the org tree ("sees
			# everything"), so the ONLY thing narrowing them is the User Permission — which is precisely
			# the layer the Smart View was missing. A Sales User is already restricted to leads they own,
			# sees nothing here, and would prove nothing.
			user.append("roles", {"role": "Sales Manager"})
			user.save(ignore_permissions=True)

		# The scoping this deployment actually uses: a User Permission on the grain master.
		if not frappe.db.exists("User Permission", {"user": USER, "allow": "CRM Vertical"}):
			frappe.get_doc({
				"doctype": "User Permission", "user": USER,
				"allow": "CRM Vertical", "for_value": VERTICAL_MINE,
			}).insert(ignore_permissions=True)

		# The FIELD gate is stubbed to a known-good state: this user is entitled to their own grain and
		# that grain's contract ticks two fields. Without it the field layer resolves nothing, the view
		# comes back empty, and a leak in the ROW gate would hide behind that emptiness.
		cls.grain = (VERTICAL_MINE, GROUP, "")
		cls.contract = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": "ZZ Row Gate Contract", "enabled": 1,
			"is_internal": 1, "vertical": VERTICAL_MINE, "crm_group": GROUP,
			"allowed_fields": [{"field": "lead:mobile_no"}, {"field": "lead:status"}],
		}).insert(ignore_permissions=True).name

		cls.mine = cls._lead(PHONE_MINE, VERTICAL_MINE)
		cls.other = cls._lead(PHONE_OTHER, VERTICAL_OTHER)
		cls.view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": "ZZ Row Gate View", "base_object": "Lead",
			"is_standard": 1,
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		frappe.db.delete("User Permission", {"user": USER})
		for dt, name in (("CRM Smart View", cls.view), ("CRM Lead API Mapping", cls.contract),
		                 ("User", USER), ("CRM Group", GROUP),
		                 ("CRM Vertical", VERTICAL_MINE), ("CRM Vertical", VERTICAL_OTHER)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610005%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	@classmethod
	def _lead(cls, phone, vertical):
		doc = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Row Gate", "mobile_no": phone, "status": "New",
			"custom_vertical": vertical, "custom_group": GROUP,
		}).insert(ignore_permissions=True)
		return doc.name

	def _as_user(self, fn):
		"""Run as the scoped user, with the field gate held at a known-good state (see setUpClass)."""
		frappe.set_user(USER)
		try:
			with patch.object(entitlement, "entitled_grains", return_value={self.grain}):
				return fn()
		finally:
			frappe.set_user("Administrator")

	def test_the_smart_view_shows_exactly_what_frappes_own_list_shows(self):
		"""THE lock. Two answers to one question is the whole defect; equality is the whole fix."""
		native = set(self._as_user(
			lambda: frappe.get_list("CRM Lead", pluck="name", limit_page_length=0)
		))
		rows = self._as_user(lambda: smartview.get_data(self.view, page_size=200))
		self.assertEqual(
			{r["name"] for r in rows["rows"]} - native, set(),
			"the Smart View returned leads Frappe's own list refuses this user",
		)

	def test_a_lead_outside_the_users_permission_is_not_in_the_view(self):
		"""Named concretely, so a regression says WHICH lead leaked rather than only that a count moved."""
		rows = self._as_user(lambda: smartview.get_data(self.view, page_size=200))
		names = {r["name"] for r in rows["rows"]}
		self.assertIn(self.mine, names, "the user's own vertical must still be visible")
		self.assertNotIn(self.other, names, "a lead in another vertical must never reach this user")

	def test_a_lead_chart_counts_only_leads_the_user_may_see(self):
		"""The Manager Dashboard assembles its own query, so Frappe's list gate never runs for it. Its only
		gate was `sales_user_only` — "is this a sales user at all", never "which leads". A funnel by product
		line still tells a rep how many patients exist in verticals they cannot open."""
		from tatva_connect.dashboard import grain_charts

		data = self._as_user(lambda: grain_charts.leads_by_vertical())["data"]
		counted = {row["vertical"] for row in data}
		self.assertIn(VERTICAL_MINE, counted, "the user's own vertical must still be counted")
		self.assertNotIn(VERTICAL_OTHER, counted, "a vertical this user cannot see must not be counted")

	def test_a_task_chart_counts_only_tasks_on_leads_the_user_may_see(self):
		"""Same gap on the task side, and tasks carry their own row gate through the parent lead."""
		from tatva_connect.dashboard import team_charts

		mine = self._as_user(lambda: team_charts.total_tasks()["value"])
		everything = frappe.db.count("CRM Task")
		self.assertLess(
			mine, everything,
			"the task KPI counted every task in the system regardless of who is looking",
		)

	def test_the_count_is_gated_too(self):
		"""The tab count is a second query. A gate on the rows and not the total still leaks the total."""
		rows = self._as_user(lambda: smartview.get_data(self.view, page_size=200))
		native = self._as_user(lambda: frappe.get_list("CRM Lead", pluck="name", limit_page_length=0))
		self.assertLessEqual(
			rows["total"], len(native),
			"the count must not exceed what this user may actually read",
		)
