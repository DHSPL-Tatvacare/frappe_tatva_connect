# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Live import that ticked in-app off assigns its leads without frappe's alert; unticked, the alert is queued as before."""
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.lead import assignment

BULK_JOB = "tatva_connect.api.partner_bulk_worker.process_job"
ALERT = "frappe.desk.doctype.notification_log.notification_log.make_notification_logs"
REP = "quiet-import-probe@example.invalid"


class TestLiveImportQuiet(IntegrationTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(setattr, frappe.local, "job", None)
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", REP):
			frappe.get_doc({"doctype": "User", "email": REP, "first_name": "Quiet Probe", "send_welcome_email": 0}).insert(ignore_permissions=True)
		self.rep = REP
		self.queued = []
		recorder = patch("frappe.enqueue", lambda method, **_kwargs: self.queued.append(str(method)))
		recorder.start()
		self.addCleanup(recorder.stop)

	def _assign_inside_import(self, skip_in_app):
		job = frappe.get_doc({"doctype": "CRM Bulk Job", "partner": "Administrator", "operation": "lead_import",
		                      "status": "InProgress", "bulk_lane": "Live", "skip_in_app": skip_in_app}).insert(ignore_permissions=True)
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "Zzquiet", "status": "New"}).insert(ignore_permissions=True).name
		frappe.local.job = frappe._dict(site=frappe.local.site, method=BULK_JOB, job_name="t", kwargs={"bulk_job_id": job.name})
		self.queued.clear()  # only the assignment's own enqueues count, never the lead insert's
		assignment.assign_for_system("CRM Lead", lead, self.rep, note="import")
		return lead

	def test_a_ticked_import_assigns_without_the_alert(self):
		lead = self._assign_inside_import(skip_in_app=1)
		self.assertTrue(frappe.db.exists("ToDo", {"reference_name": lead, "allocated_to": self.rep, "status": "Open"}))
		self.assertNotIn(ALERT, self.queued, "a ticked import still queued frappe's assignment alert")

	def test_an_unticked_import_queues_the_alert(self):
		self._assign_inside_import(skip_in_app=0)
		self.assertIn(ALERT, self.queued, "an unticked import lost frappe's assignment alert")
