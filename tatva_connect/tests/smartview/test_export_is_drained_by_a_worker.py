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
feed is pinned beside it, because that is what a tab reads on load to reach a file it missed.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_export_is_drained_by_a_worker
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import exports, job_feed
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

	def setUp(self):
		# The drain runs inline, so RQ answers as it would for the job a worker is running.
		patch.object(job_feed, "is_job_enqueued", return_value=True).start()
		self.addCleanup(patch.stopall)

	def _drained(self, fmt="csv"):
		"""Queue one export and run the worker inline, capturing what it published."""
		queued = smartview.export_view(self.view, fmt)
		with patch.object(frappe, "publish_realtime") as published:
			exports.run(queued["job"])
		events = [c for c in published.call_args_list if c.args[0] == job_feed.EVENT]
		return frappe.get_doc(exports.DOCTYPE, queued["job"]), events

	def test_the_file_is_the_screen_when_a_row_limit_is_asked_for(self):
		"""The export is what the READER is looking at. A Smart View download used to ignore the screen and
		walk every matching page to the ceiling, so a reader with thirty rows in front of them got a file of
		a hundred thousand — while the native list beside it exported exactly what was loaded. Same dialog
		now, so the same question must get the same answer."""
		queued = smartview.export_view(self.view, "csv", limit=3)
		with patch.object(frappe, "publish_realtime"):
			exports.run(queued["job"])
		job = frappe.get_doc(exports.DOCTYPE, queued["job"])
		self.assertEqual(job.status, "Completed", job.error_message)
		self.assertLessEqual(job.row_count, 3)

	def test_asking_for_the_screen_is_not_reported_as_truncated(self):
		"""`truncated` is the CEILING speaking, and the tab turns it into "only the first N rows were
		exported". A reader who asked for the rows on screen got precisely what they asked for, so saying
		that would be a lie about their own choice — and the one that trains people to distrust the warning
		on the day it is real."""
		# The ceiling is forced DOWN to the asked-for size, so only the limit keeps `truncated` false.
		queued = smartview.export_view(self.view, "csv", limit=1)
		with patch.object(exports, "row_cap", return_value=1):
			with patch.object(frappe, "publish_realtime") as published:
				exports.run(queued["job"])
		ready = _with_status(published.call_args_list, "Completed")
		self.assertTrue(ready, "the worker published no ready event")
		self.assertFalse(ready[-1].args[1].get("truncated"),
		                 "the reader asked for the rows on screen and was told the file was cut short")

	def test_no_limit_still_walks_to_the_operators_ceiling(self):
		"""Ticking "all records" must behave exactly as this endpoint did before a limit existed: the
		operator's `max_report_rows` is the only bound, and nothing here holds a number of its own."""
		with patch.object(exports, "row_cap", return_value=2) as cap:
			queued = smartview.export_view(self.view, "csv")
			with patch.object(frappe, "publish_realtime"):
				exports.run(queued["job"])
		cap.assert_called()
		job = frappe.get_doc(exports.DOCTYPE, queued["job"])
		self.assertEqual(job.status, "Completed", job.error_message)
		self.assertLessEqual(job.row_count, 2)

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
		ready = _with_status(events, "Completed")
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
		failed = _with_status(published.call_args_list, "Error")
		self.assertEqual(len(failed), 1)
		self.assertNotIn("boom", frappe.as_json(failed[0].args[1]),
		                 "a traceback reached the rep's screen")

	def test_the_url_the_tab_gets_is_one_a_browser_SAVES(self):
		"""FOUND ON PROD. The drain completed, the file was written, and the tab did nothing.

		An offloaded File's stored `file_url` is a proxy that REDIRECTS to Azure, and the HTML `download`
		attribute is same-origin-only — so it was silently ignored the moment the hop left this origin and
		the click became a navigation instead of a save. `blob_store` states the cure and states that the
		flag is never stored on the File row, because a second spelling would orphan it from
		`by_blob_key`: it is added by whoever renders the download. This seam is that control.

		Asserted on `_saveable` directly, so it holds whether or not this bench offloads to Azure."""
		from tatva_connect.storage import blob_store

		stored = blob_store.download_url("platform/crm_export_job/abc/x.xlsx")
		saveable = exports._saveable(stored)

		self.assertIn(f"&{blob_store.DOWNLOAD_FLAG}=1", saveable, "the tab was handed a link it cannot save")
		self.assertEqual(
			blob_store.blob_key_from_url(saveable),
			blob_store.blob_key_from_url(stored),
			"the save flavour names a different blob — the File row would be orphaned",
		)

	def test_a_file_still_on_local_disk_is_handed_back_untouched(self):
		"""Same origin already, so `<a download>` works and there is no redirect to sign a header onto."""
		self.assertEqual(exports._saveable("/private/files/x.xlsx"), "/private/files/x.xlsx")
		self.assertIsNone(exports._saveable(None))

	def test_a_tab_that_was_away_can_pick_its_own_exports_back_up(self):
		"""THE recovery. A drain outlives the surface that asked for it, so a tab that loads later reads the
		feed and finds the finished export WITH its file."""
		job, _events = self._drained()
		found = next((r for r in job_feed.feed()["items"] if r["job"] == job.name), None)
		self.assertTrue(found, "the tab cannot find the export it missed")
		self.assertEqual(found["status"], "Completed")
		self.assertTrue(found["file_url"], "a recovered export has no file to fetch")
		self.assertTrue(found["file_name"])

	def test_it_answers_with_the_callers_own_exports_and_nobody_elses(self):
		"""The feed means mine. An operator holds `if_owner=0`, so without the owner filter it would hand an
		administrator the whole site's exports."""
		job, _events = self._drained()
		frappe.set_user(STRANGER)
		try:
			self.assertNotIn(job.name, [r["job"] for r in job_feed.feed()["items"]],
			                 "a stranger was offered someone else's export")
		finally:
			frappe.set_user("Administrator")
		self.assertIn(job.name, [r["job"] for r in job_feed.feed()["items"]], "the owner lost their own export")

	def test_status_carries_creation_source_and_reference(self):
		"""The panel sorts a merged list of bulk jobs and exports by time and shows a human title from
		`source`/`reference`, so `_result` must carry all three on a real, completed job."""
		job, _events = self._drained()

		out = exports._result(job)
		self.assertIn("creation", out)
		self.assertIsNotNone(out["creation"])
		self.assertEqual(out["creation"], job.creation)
		self.assertEqual(out["source"], "Smart View")
		self.assertEqual(out["source"], job.source)
		self.assertEqual(out["reference"], job.reference)
		self.assertIsNotNone(out["reference"])


def _with_status(calls, status):
	return [c for c in calls if c.args[0] == job_feed.EVENT and c.args[1].get("status") == status]
