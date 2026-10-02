# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Which leads and tasks a user may see has one answer: the Smart View, its count and the dashboard agree with
Frappe's own list. Own user, User Permission, grain, contract, leads and tasks; nothing patched."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.smartview import api as smartview

VERTICAL_MINE = "ZZ Row Gate Mine"
VERTICAL_OTHER = "ZZ Row Gate Other"
GROUP = "ZZ Row Gate Group"
USER = "zz-row-gate@example.com"
PHONE_MINE = "+916100050001"
PHONE_OTHER = "+916100050002"
REGISTRY = "Access::Grain::registry"
TASK_SWITCH = "Task::CRM Task::visibility"


class TestRowGateHasOneAnswer(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._flag_was = frappe.db.get_value("CRM Tatva Automation", REGISTRY, "enabled")
		cls._switch(REGISTRY, 0)
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
			# Sales Manager, so the User Permission is the only thing narrowing what this user sees.
			user.append("roles", {"role": "Sales Manager"})
			user.save(ignore_permissions=True)

		# The scoping this deployment actually uses: a User Permission on the grain master.
		if not frappe.db.exists("User Permission", {"user": USER, "allow": "CRM Vertical"}):
			frappe.get_doc({
				"doctype": "User Permission", "user": USER,
				"allow": "CRM Vertical", "for_value": VERTICAL_MINE,
			}).insert(ignore_permissions=True)

		# The field gate, entitled the production way: an Assignment Rule at this user's grain.
		frappe.get_doc({
			"doctype": "Assignment Rule", "name": "zz-row-gate-rule", "document_type": "CRM Lead", "assign_condition": "1",
			"rule": "Round Robin", "priority": 0, "disabled": 0, "grain_vertical": VERTICAL_MINE, "grain_group": GROUP,
			"users": [{"user": USER}], "assignment_days": [{"day": "Monday"}],
		}).insert(ignore_permissions=True)
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

	@classmethod
	def tearDownClass(cls):
		cls._switch(REGISTRY, cls._flag_was)
		super().tearDownClass()

	@staticmethod
	def _switch(key, enabled):
		row = frappe.get_doc("CRM Tatva Automation", key)
		row.enabled = enabled
		row.save(ignore_permissions=True)

	@classmethod
	def _lead(cls, phone, vertical):
		doc = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Row Gate", "mobile_no": phone, "status": "New",
			"custom_vertical": vertical, "custom_group": GROUP,
		}).insert(ignore_permissions=True)
		return doc.name

	def _as_user(self, fn):
		with self.set_user(USER):
			return fn()

	def test_the_smart_view_shows_exactly_what_frappes_own_list_shows(self):
		"""The Smart View never returns a lead that Frappe's own list refuses this user."""
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

	def _declared(self, chart_name):
		"""The seeded declaration, read as the endpoint reads it, so the test drives the real door."""
		from tatva_connect.dashboard import declaration

		rows = frappe.get_list(
			"CRM Dashboard Chart", filters={"name": chart_name}, fields=list(declaration.READ), limit=1
		)
		self.assertTrue(rows, f"{chart_name} ships in dashboard/seed.py and must exist after migrate")
		return rows[0]

	def test_a_lead_chart_counts_only_leads_the_user_may_see(self):
		"""A lead chart counts only the verticals this user can open, because it runs through `frappe.get_list`."""
		from tatva_connect.dashboard import executor

		chart = self._declared("leads_by_vertical")
		points = self._as_user(lambda: executor.run(chart))["points"]
		counted = {point["raw"] for point in points}
		self.assertIn(VERTICAL_MINE, counted, "the user's own vertical must still be counted")
		self.assertNotIn(VERTICAL_OTHER, counted, "a vertical this user cannot see must not be counted")

	def test_a_task_chart_counts_only_tasks_on_leads_the_user_may_see(self):
		"""A task on a lead the user cannot see never moves their count; one on their own lead adds exactly one."""
		from tatva_connect.dashboard import executor

		self._switch(TASK_SWITCH, 1)
		chart = self._declared("total_tasks")
		count = lambda: self._as_user(lambda: executor.run(chart)["value"])  # noqa: E731
		before = count()
		self._task(self.other)
		self.assertEqual(count(), before, "a task on a lead this user cannot see was counted")
		self._task(self.mine)
		self.assertEqual(count(), before + 1, "a task on the user's own lead must be counted")

	@staticmethod
	def _task(lead):
		frappe.get_doc({"doctype": "CRM Task", "title": "ZZ Row Gate Task", "status": "Todo",
		                "reference_doctype": "CRM Lead", "reference_docname": lead}).insert(ignore_permissions=True)

	def test_the_count_is_gated_too(self):
		"""The tab count is a separate query, so it is gated too and never leaks the total."""
		rows = self._as_user(lambda: smartview.get_data(self.view, page_size=200))
		native = self._as_user(lambda: frappe.get_list("CRM Lead", pluck="name", limit_page_length=0))
		self.assertLessEqual(
			rows["total"], len(native),
			"the count must not exceed what this user may actually read",
		)
