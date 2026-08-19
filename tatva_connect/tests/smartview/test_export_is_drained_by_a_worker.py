# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An export is drained by a worker, and the person who asked can always get the file.

THE DEFECT THIS LOCKS. Every export in the SPA built its file inside the HTTP request. Measured on prod
for a Sales Manager inside `CRM Sales Hierarchy` with two User Permissions, one Smart View export cost
~41.7s of SQL on top of ~25,000 per-row permission round trips, and died on the 120s Azure Application
Gateway timeout as a 504 HTML page. A row ceiling only buys headroom; the request is the wrong place.

WHAT IS ASSERTED HERE IS THE SEAM, NOT SOCKETIO. This app's responsibility ends at
`frappe.publish_realtime` — delivery is frappe's own transport, already carrying notifications, telephony
and workflow steps in production. So the event, its payload and its `user=` targeting are pinned, and the
POLL path is pinned beside it, because that is what makes the file reachable when the socket is down.
Recovery after a closed tab is Desk's own `if_owner` list on the job doctype, so there is no endpoint
of ours to test for it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_export_is_drained_by_a_worker
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import exports
from tatva_connect.smartview import api as smartview

STRANGER = "zz-export-stranger@example.com"


class TestExportIsDrainedByAWorker(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", STRANGER):
			frappe.get_doc({"doctype": "User", "email": STRANGER, "first_name": "Export Stranger",
			                "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}
			               ).insert(ignore_permissions=True)
		cls.view = frappe.get_all("CRM Smart View", filters={"base_object": "Lead"},
		                          limit=1, pluck="name")[0]
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in frappe.get_all(exports.DOCTYPE, pluck="name"):
			# Files first: deleting the job cascades to its attachment, and the two racing is a lock error.
			for f in frappe.get_all("File", filters={"attached_to_doctype": exports.DOCTYPE,
			                                         "attached_to_name": name}, pluck="name"):
				frappe.delete_doc("File", f, force=True, ignore_permissions=True, delete_permanently=True)
			frappe.db.commit()
			frappe.delete_doc(exports.DOCTYPE, name, force=True, ignore_permissions=True)
			frappe.db.commit()
		if frappe.db.exists("User", STRANGER):
			frappe.delete_doc("User", STRANGER, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _drained(self, fmt="csv"):
		"""Queue one export and run the worker inline, capturing what it published."""
		queued = smartview.export_view(self.view, fmt)
		with patch.object(frappe, "publish_realtime") as published:
			exports.run(queued["job"])
		events = [c for c in published.call_args_list if str(c.args[0]).startswith("crm_export_")]
		return frappe.get_doc(exports.DOCTYPE, queued["job"]), events

	def test_the_request_only_queues(self):
		"""THE 504. The endpoint must answer at once, and never build the file on the way."""
		with patch.object(smartview.tabular, "write") as write:
			queued = smartview.export_view(self.view, "csv")
		write.assert_not_called()
		self.assertEqual(queued["status"], "Queued")
		self.assertEqual(frappe.db.get_value(exports.DOCTYPE, queued["job"], "source"), "Smart View")

	def test_the_worker_produces_a_private_file_owned_by_the_job(self):
		"""M1: the file is born owned by a real record, and privacy is DERIVED, never asked for."""
		job, _events = self._drained("xlsx")
		self.assertEqual(job.status, "Completed", job.error_message)
		file = frappe.db.get_value(
			"File", {"attached_to_doctype": exports.DOCTYPE, "attached_to_name": job.name},
			["file_name", "is_private", "file_size"], as_dict=True,
		)
		self.assertTrue(file, "the drain produced no file")
		self.assertEqual(file.is_private, 1, "an export was written as a PUBLIC file")
		self.assertTrue(file.file_name.endswith(".xlsx"))
		self.assertGreater(file.file_size, 0)

	def test_it_tells_the_person_who_asked_and_nobody_else(self):
		"""An export is one person's answer. A broadcast would put it in every open tab."""
		job, events = self._drained()
		ready = [c for c in events if c.args[0] == exports.EVENT_READY]
		self.assertEqual(len(ready), 1, "the tab was never told the export was ready")
		self.assertEqual(ready[0].kwargs.get("user"), job.owner,
		                 "the ready event was not targeted at the person who asked")
		self.assertEqual(ready[0].args[1]["job"], job.name)

	def test_a_failed_drain_is_recorded_and_reported(self):
		"""A worker that dies silently is worse than the 504 it replaced."""
		queued = smartview.export_view(self.view, "csv")
		boom = patch.object(smartview, "produce_export", side_effect=RuntimeError("boom"))
		with boom, patch.object(frappe, "publish_realtime") as published:
			exports.run(queued["job"])
		job = frappe.get_doc(exports.DOCTYPE, queued["job"])
		self.assertEqual(job.status, "Error")
		self.assertIn("boom", job.error_message)
		failed = [c for c in published.call_args_list if c.args[0] == exports.EVENT_FAILED]
		self.assertEqual(len(failed), 1)
		self.assertNotIn("boom", frappe.as_json(failed[0].args[1]),
		                 "a traceback reached the rep's screen")

	def test_status_is_the_socket_free_path_to_the_file(self):
		"""THE GUARANTEE. With socketio down the tab polls this, so the file is still reachable."""
		job, _events = self._drained()
		state = exports.status(job.name)
		self.assertEqual(state["status"], "Completed")
		self.assertTrue(state["file_url"], "the poll path cannot reach the file")
		self.assertTrue(state["file_name"])

	def test_another_rep_cannot_reach_someone_elses_export(self):
		"""`if_owner` is the gate on both the row and, through it, the file."""
		job, _events = self._drained()
		frappe.set_user(STRANGER)
		try:
			with self.assertRaises(frappe.PermissionError):
				exports.status(job.name)
		finally:
			frappe.set_user("Administrator")
