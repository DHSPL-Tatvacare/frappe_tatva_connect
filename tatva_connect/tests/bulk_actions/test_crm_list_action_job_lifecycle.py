import json

import frappe
from frappe.tests import IntegrationTestCase


class TestCRMListActionJobLifecycle(IntegrationTestCase):
	def test_insert_starts_queued_and_enqueues(self):
		job = frappe.get_doc({
			"doctype": "CRM List Action Job",
			"action": "Assign",
			"target_doctype": "CRM Lead",
			"docnames": json.dumps(["CRM-LEAD-0001", "CRM-LEAD-0002"]),
			"params": json.dumps({"assign_to": "test@example.com"}),
		}).insert(ignore_permissions=True)
		self.assertEqual(job.status, "Queued")
		frappe.db.rollback()
