# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A guest on a public dashboard lists a filter's values; the same guest is refused on a private dashboard and on a column that is not a filter."""
import frappe
from frappe.tests import IntegrationTestCase
from insights.api import run_doc_method

DASHBOARD = "Insights Dashboard v3"


class TestAGuestListsAPublicDashboardsFilterValues(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.get_doc({"doctype": "ToDo", "description": "public filter probe", "status": "Open"}).insert()
		workbook = frappe.get_doc({"doctype": "Insights Workbook", "title": "Public filter probe"}).insert().name
		self.query = frappe.get_doc({
			"doctype": "Insights Query v3", "title": "ToDos", "workbook": workbook,
			"use_live_connection": 1, "is_builder_query": 1,
			"operations": [{"type": "source", "table": {"type": "table", "data_source": "Site DB", "table_name": "tabToDo"}}],
		}).insert().name
		chart = frappe.get_doc({
			"doctype": "Insights Chart v3", "title": "ToDos", "workbook": workbook,
			"query": self.query, "chart_type": "Bar", "config": {},
		}).insert().name
		self.dashboard = frappe.get_doc({
			"doctype": DASHBOARD, "title": "ToDos", "workbook": workbook,
			"items": [
				{"id": "chart-1", "type": "chart", "chart": chart},
				{"id": "filter-1", "type": "filter", "filter_name": "Status", "filter_type": "String",
				 "links": {chart: f"`{self.query}`.`status`"}},
			],
		}).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _publish(self):
		self.dashboard.update_access({"is_public": 1})

	def _values_as_guest(self, column="status"):
		frappe.set_user("Guest")
		frappe.local.response = frappe._dict(docs=[])
		try:
			return run_doc_method(
				"get_distinct_column_values",
				{"doctype": DASHBOARD, "name": self.dashboard.name},
				{"query": self.query, "column_name": column},
			)
		finally:
			frappe.set_user("Administrator")

	def test_a_guest_lists_a_public_filters_values(self):
		self._publish()
		self.assertIn("Open", self._values_as_guest())

	def test_a_guest_is_refused_a_private_dashboard(self):
		with self.assertRaises(frappe.PermissionError):
			self._values_as_guest()

	def test_a_guest_is_refused_a_column_that_is_not_a_filter(self):
		self._publish()
		with self.assertRaises(frappe.PermissionError):
			self._values_as_guest(column="description")
