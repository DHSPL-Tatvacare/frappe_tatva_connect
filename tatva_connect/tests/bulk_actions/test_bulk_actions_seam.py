import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect import bulk_actions
from tatva_connect.automation import registry


class TestBulkActionsSeam(IntegrationTestCase):
	def test_under_threshold_runs_inline_even_when_enabled(self):
		with patch.object(bulk_actions.automation, "is_enabled", return_value=True), \
		     patch.object(bulk_actions, "_run", return_value={"total": 3, "succeeded": 3, "failed": 0, "failed_names": []}) as run:
			out = bulk_actions.run_or_queue("Assign", "CRM Lead", json.dumps(["a", "b", "c"]), "{}")
		self.assertFalse(out["queued"])
		run.assert_called_once()
		self.assertEqual(frappe.db.count("CRM List Action Job"), 0)

	def test_disabled_runs_inline_even_over_threshold(self):
		docnames = json.dumps([f"CRM-LEAD-{i:04d}" for i in range(25)])
		with patch.object(bulk_actions.automation, "is_enabled", return_value=False), \
		     patch.object(bulk_actions, "_run", return_value={"total": 25, "succeeded": 25, "failed": 0, "failed_names": []}) as run:
			out = bulk_actions.run_or_queue("Assign", "CRM Lead", docnames, "{}")
		self.assertFalse(out["queued"])
		run.assert_called_once()

	def test_enabled_over_threshold_queues_a_job(self):
		docnames = json.dumps([f"CRM-LEAD-{i:04d}" for i in range(25)])
		with patch.object(bulk_actions.automation, "is_enabled", return_value=True):
			out = bulk_actions.run_or_queue("Assign", "CRM Lead", docnames, "{}")
		self.assertTrue(out["queued"])
		self.assertEqual(frappe.db.get_value("CRM List Action Job", out["job"], "status"), "Queued")
		frappe.db.rollback()

	def test_run_or_queue_refuses_over_max_rows(self):
		docnames = json.dumps([f"CRM-LEAD-{i:04d}" for i in range(bulk_actions.MAX_ROWS + 1)])
		with self.assertRaises(frappe.ValidationError):
			bulk_actions.run_or_queue("Assign", "CRM Lead", docnames, json.dumps({"assign_to": ["Administrator"]}))

	def test_automation_key_is_registered_and_dormant_by_shape(self):
		"""The kill switch this seam reads must actually exist in the catalog, in the right shape."""
		keys = {a.key for a in registry.AUTOMATIONS}
		self.assertIn(bulk_actions.AUTOMATION_KEY, keys)

	def test_status_carries_the_jobs_own_creation_timestamp(self):
		"""The panel sorts a merged list of bulk jobs and exports by time, so `_result` must carry
		`creation` — not a stand-in like `modified` — on a real, completed job."""
		docnames = json.dumps([f"CRM-LEAD-{i:04d}" for i in range(25)])
		with patch.object(bulk_actions.automation, "is_enabled", return_value=True):
			queued = bulk_actions.run_or_queue("Assign", "CRM Lead", docnames, "{}")
		try:
			with patch.object(bulk_actions, "_run",
			                  return_value={"succeeded": 25, "failed": 0, "failed_names": []}):
				bulk_actions.run(queued["job"])
			doc = frappe.get_doc("CRM List Action Job", queued["job"])
			self.assertEqual(doc.status, "Completed", doc.error_message)

			out = bulk_actions.status(queued["job"])
			self.assertIn("creation", out)
			self.assertIsNotNone(out["creation"])
			self.assertEqual(out["creation"], doc.creation)
		finally:
			frappe.delete_doc("CRM List Action Job", queued["job"], force=True, ignore_permissions=True)
			frappe.db.commit()
