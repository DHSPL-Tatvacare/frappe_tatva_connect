# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A dashboard shared without its workbook still reaches the spotlight: every read a source makes passes the one gate."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.search import shortcuts

REP = "zz-spotlight-shared@example.test"
FAILED = "Spotlight shortcut source failed"


class TestSpotlightSharedDashboard(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", REP):
			frappe.get_doc({
				"doctype": "User", "email": REP, "first_name": "ZZ Spotlight", "send_welcome_email": 0,
				"roles": [{"role": "Sales User"}],
			}).insert(ignore_permissions=True)
		book = frappe.get_doc({"doctype": "Insights Workbook", "title": "ZZ Shared Book"}).insert()
		self.dashboard = frappe.get_doc(
			{"doctype": "Insights Dashboard v3", "title": "ZZ Shared Dashboard", "workbook": book.name, "items": "[]"}
		).insert()
		frappe.share.add("Insights Dashboard v3", self.dashboard.name, REP, read=1, notify=0)

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_a_rep_with_only_a_shared_dashboard_gets_it_and_no_error(self):
		"""No Insights role, one dashboard shared: it is listed without a workbook subtitle, and nothing is logged."""
		failures = frappe.db.count("Error Log", {"method": FAILED})
		frappe.set_user(REP)
		found = shortcuts.shortcuts()["shortcuts"]
		frappe.set_user("Administrator")

		dashboards = [row for row in found if row["kind"] == shortcuts.KIND_DASHBOARD]
		self.assertIn("ZZ Shared Dashboard", [row["label"] for row in dashboards])
		self.assertEqual(frappe.db.count("Error Log", {"method": FAILED}), failures, "a source must not raise for a shared dashboard")
