# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""End-to-end proof of the async bulk-job tier (Phase 1): submit -> worker drains -> status/results,
partial success, dormant gate, owner-scoping, and clean cancel.

The worker COMMITS as it goes (real leads land), so every created lead is torn down explicitly; the
job cascades its results and payload file on delete. `frappe.enqueue` is patched off so the submit's
after-commit hook never fires — the worker is driven in-process instead.
"""
import base64
import json
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from tatva_connect.api import partner_bulk_job, partner_bulk_worker

PARTNER = "bulkjob.lock.partner@example.test"
OTHER = "bulkjob.lock.other@example.test"
VERTICAL, GROUP = "GoodFlip Care", "Anaya"
TOGGLE = "Partner::AsyncBulk::jobs"
NAME_PREFIX = "BulkJobTest"


def _mint_partner(email, vertical=VERTICAL, group=GROUP):
	if not frappe.db.exists("User", email):
		frappe.get_doc({"doctype": "User", "email": email, "first_name": "Bulk Lock",
		                "send_welcome_email": 0, "user_type": "System User"}).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	if "Partner API User" not in [r.role for r in user.roles]:
		user.append("roles", {"role": "Partner API User"})
		user.save(ignore_permissions=True)
	# The contract is keyed on its grain composite, so it is found by the partner_user COLUMN, never by name.
	if not frappe.db.exists("CRM Lead API Mapping", {"partner_user": email}):
		frappe.get_doc({"doctype": "CRM Lead API Mapping", "partner_user": email, "enabled": 1,
		                "contract_name": email, "vertical": vertical,
		                "crm_group": group}).insert(ignore_permissions=True)


def _purge_test_leads():
	"""Clear the test's distinctive mobile range and name prefix so every create is genuinely fresh."""
	names = set(frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610000%"]}, pluck="name"))
	names |= set(frappe.get_all("CRM Lead", filters={"first_name": ["like", f"{NAME_PREFIX}%"]}, pluck="name"))
	for name in names:
		frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)


class TestPartnerAsyncBulkJobs(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_mint_partner(PARTNER)
		_mint_partner(OTHER)  # same grain; owner-scoping is per-user, not per-grain
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1)
		_purge_test_leads()  # a distinctive test-only mobile range, cleared so every create is fresh
		frappe.db.commit()  # survives the per-test rollback; the worker/gate need it live

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_purge_test_leads()
		for job in frappe.get_all("CRM Bulk Job", filters={"partner": ["in", (PARTNER, OTHER)]}, pluck="name"):
			frappe.delete_doc("CRM Bulk Job", job, force=True, ignore_permissions=True)
		for email in (PARTNER, OTHER):
			for contract in frappe.get_all("CRM Lead API Mapping", filters={"partner_user": email}, pluck="name"):
				frappe.delete_doc("CRM Lead API Mapping", contract, force=True, ignore_permissions=True)
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 0)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user(PARTNER)

	def tearDown(self):
		frappe.set_user("Administrator")
		# reset the test partners' jobs so the per-partner concurrency cap doesn't accumulate across tests
		for job in frappe.get_all("CRM Bulk Job", filters={"partner": ["in", (PARTNER, OTHER)]}, pluck="name"):
			frappe.delete_doc("CRM Bulk Job", job, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _submit(self, operation, records, user=PARTNER):
		frappe.set_user(user)
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(operation=operation, format="inline", records=records)
		with patch("frappe.enqueue"):
			partner_bulk_job.create()
		return frappe.local.response

	def _lead(self, i, first="P"):
		return {"mobile_no": f"+91610000{i:04d}", "first_name": f"{NAME_PREFIX}{first}{i}"}

	def test_inline_lead_job_lands_rows_and_results(self):
		resp = self._submit("lead_create", [self._lead(i) for i in range(3)])
		self.assertEqual(resp["http_status_code"], 202)
		job_id = resp["data"]["job_id"]
		self.assertEqual(resp["data"]["status"], "UploadComplete")

		partner_bulk_worker.process_job(job_id)
		frappe.set_user("Administrator")

		job = frappe.get_doc("CRM Bulk Job", job_id)
		self.assertEqual(job.status, "JobComplete")
		self.assertEqual((job.total, job.processed, job.succeeded, job.failed), (3, 3, 3, 0))
		# the rows really landed, on the caller's grain
		landed = frappe.get_all("CRM Lead", filters={"first_name": ["like", f"{NAME_PREFIX}P%"],
		                        "custom_vertical": VERTICAL, "custom_group": GROUP}, pluck="name")
		self.assertEqual(len(landed), 3)
		results = frappe.get_all("CRM Bulk Job Result", filters={"job": job_id},
		                         fields=["action", "record_name"])
		self.assertEqual(sorted(r.action for r in results), ["created", "created", "created"])
		self.assertTrue(all(r.record_name in landed for r in results))

	def test_partial_success_a_bad_record_fails_alone(self):
		# a lead with no mobile_no fails validation; the good ones still land, job still completes
		recs = [self._lead(10, "Q"), {"first_name": f"{NAME_PREFIX}NoPhone"}, self._lead(11, "Q")]
		resp = self._submit("lead_create", recs)
		partner_bulk_worker.process_job(resp["data"]["job_id"])
		frappe.set_user("Administrator")
		job = frappe.get_doc("CRM Bulk Job", resp["data"]["job_id"])
		self.assertEqual(job.status, "JobComplete")
		self.assertEqual((job.succeeded, job.failed), (2, 1))
		fail = frappe.get_all("CRM Bulk Job Result", filters={"job": job.name, "action": "failed"},
		                      fields=["record_index"])
		self.assertEqual([r.record_index for r in fail], [1])

	def test_dormant_tier_refuses_submit(self):
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 0)
		try:
			resp = self._submit("lead_create", [self._lead(20)])
			self.assertEqual(resp["error"]["code"], "forbidden")
			self.assertEqual(resp["http_status_code"], 403)
		finally:
			frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1)

	def test_owner_scoping_hides_another_partners_job(self):
		resp = self._submit("lead_create", [self._lead(30)], user=PARTNER)
		job_id = resp["data"]["job_id"]
		frappe.set_user(OTHER)
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(job_id=job_id)
		partner_bulk_job.get()
		self.assertEqual(frappe.local.response["error"]["code"], "not_found")

	def test_clean_cancel_deletes_only_created_rows(self):
		resp = self._submit("lead_create", [self._lead(40, "C"), self._lead(41, "C")])
		job_id = resp["data"]["job_id"]
		partner_bulk_worker.process_job(job_id)  # creates 2 leads
		frappe.set_user("Administrator")
		created = frappe.get_all("CRM Bulk Job Result", filters={"job": job_id, "action": "created"},
		                         pluck="record_name")
		self.assertEqual(len(created), 2)
		self.assertTrue(all(frappe.db.exists("CRM Lead", n) for n in created))

		partner_bulk_worker._compensate_and_abort(frappe.get_doc("CRM Bulk Job", job_id))
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("CRM Bulk Job", job_id, "status"), "Aborted")
		self.assertFalse(any(frappe.db.exists("CRM Lead", n) for n in created))

	# -- Phase 2: file jobs --------------------------------------------------

	def _submit_file(self, operation, fmt, content, user=PARTNER):
		frappe.set_user(user)
		frappe.local.response = frappe._dict()
		raw = content.encode() if isinstance(content, str) else content
		frappe.form_dict = frappe._dict(operation=operation, format=fmt,
		                                content_base64=base64.b64encode(raw).decode())
		with patch("frappe.enqueue"):
			partner_bulk_job.create()
		return frappe.local.response

	def test_file_jsonl_lands_like_inline(self):
		content = "\n".join(json.dumps(self._lead(50 + i, "J")) for i in range(3))
		resp = self._submit_file("lead_create", "jsonl", content)
		self.assertEqual(resp["http_status_code"], 202)
		partner_bulk_worker.process_job(resp["data"]["job_id"])
		frappe.set_user("Administrator")
		job = frappe.get_doc("CRM Bulk Job", resp["data"]["job_id"])
		self.assertEqual((job.status, job.total, job.succeeded), ("JobComplete", 3, 3))
		self.assertEqual(len(frappe.get_all("CRM Lead", {"first_name": ["like", f"{NAME_PREFIX}J%"]})), 3)

	def test_file_csv_lead_core_lands(self):
		rows = [self._lead(60 + i, "V") for i in range(3)]
		content = "mobile_no,first_name\n" + "\n".join(f"{r['mobile_no']},{r['first_name']}" for r in rows)
		resp = self._submit_file("lead_create", "csv", content)
		self.assertEqual(resp["http_status_code"], 202)
		partner_bulk_worker.process_job(resp["data"]["job_id"])
		frappe.set_user("Administrator")
		job = frappe.get_doc("CRM Bulk Job", resp["data"]["job_id"])
		self.assertEqual((job.status, job.succeeded), ("JobComplete", 3))
		self.assertEqual(len(frappe.get_all("CRM Lead", {"first_name": ["like", f"{NAME_PREFIX}V%"]})), 3)

	def test_csv_is_refused_for_activity(self):
		resp = self._submit_file("activity_create", "csv", "a,b\n1,2")
		self.assertEqual((resp["error"]["code"], resp["http_status_code"]), ("validation_error", 400))

	def test_oversized_file_refused_at_ingress(self):
		frappe.db.set_single_value("CRM Partner API Settings", "async_file_max_mb", 1)
		try:
			resp = self._submit_file("lead_create", "jsonl", "x" * (1024 * 1024 + 16))
			self.assertEqual((resp["error"]["code"], resp["http_status_code"]), ("validation_error", 400))
			self.assertIn("MB limit", resp["error"]["message"])
		finally:
			frappe.db.set_single_value("CRM Partner API Settings", "async_file_max_mb", 50)

	def test_file_over_record_cap_is_failed_and_purged(self):
		frappe.db.set_single_value("CRM Partner API Settings", "async_file_max_records", 2)
		try:
			content = "\n".join(json.dumps(self._lead(70 + i, "K")) for i in range(3))  # 3 > cap 2
			resp = self._submit_file("lead_create", "jsonl", content)
			job_id = resp["data"]["job_id"]
			partner_bulk_worker.process_job(job_id)
			frappe.set_user("Administrator")
			job = frappe.get_doc("CRM Bulk Job", job_id)
			self.assertEqual((job.status, job.succeeded), ("Failed", 0))  # rejected whole, nothing parsed
			self.assertFalse(frappe.db.exists(
				"File", {"attached_to_doctype": "CRM Bulk Job", "attached_to_name": job_id}))
			self.assertEqual(len(frappe.get_all("CRM Lead", {"first_name": ["like", f"{NAME_PREFIX}K%"]})), 0)
		finally:
			frappe.db.set_single_value("CRM Partner API Settings", "async_file_max_records", 50000)

	def _workbook_job(self, rows):
		"""A real job row carrying a workbook, read back as xlsx. Returns (job, raw).

		The row is inserted as csv and read as xlsx: the record cap is what is under test, and the
		doctype's own Select is a separate concern that would only be exercised after a migrate."""
		from tatva_connect import tabular
		raw = tabular.write(["mobile_no", "first_name"], rows, "xlsx")
		with patch("frappe.enqueue"):
			name = partner_bulk_job.submit_job(PARTNER, "lead_create", "csv", raw)
		job = frappe.get_doc("CRM Bulk Job", name)
		job.input_format = "xlsx"
		return job, raw

	def test_a_workbook_is_not_rejected_by_a_newline_count(self):
		"""An xlsx is a ZIP, so its 0x0A bytes are binary noise and say nothing about its row count.

		The cheap pre-check is sound for a line-based payload and meaningless for a workbook: at roughly
		one newline byte in 256, a large-but-shallow workbook would be refused for a limit it never
		reached. Three rows under a cap of five, in a file whose binary carries more than five newlines."""
		frappe.set_user("Administrator")
		job, raw = self._workbook_job([[f"+91610000{80 + i:04d}", f"{NAME_PREFIX}X{i}"] for i in range(3)])
		self.assertGreater(raw.count(b"\n"), 5, "fixture does not exercise the newline pre-check")
		frappe.db.set_single_value("CRM Partner API Settings", "async_file_max_records", 5)
		try:
			self.assertEqual(len(partner_bulk_worker._to_batch(job, raw)), 3)
		finally:
			frappe.db.set_single_value("CRM Partner API Settings", "async_file_max_records", 50000)

	def test_a_workbook_over_the_record_cap_is_still_refused(self):
		"""The true count is enforced after parsing, for every format — not only the cheap bound."""
		frappe.set_user("Administrator")
		job, raw = self._workbook_job([[f"+91610000{90 + i:04d}", f"{NAME_PREFIX}Y{i}"] for i in range(4)])
		frappe.db.set_single_value("CRM Partner API Settings", "async_file_max_records", 2)
		try:
			with self.assertRaises(partner_bulk_worker.PayloadRejected):
				partner_bulk_worker._to_batch(job, raw)
		finally:
			frappe.db.set_single_value("CRM Partner API Settings", "async_file_max_records", 50000)

	def test_ragged_csv_row_fails_alone(self):
		content = ("mobile_no,first_name\n"
		           f"+916100000080,{NAME_PREFIX}R80\n"
		           f"+916100000081,{NAME_PREFIX}R81,EXTRA\n"  # ragged: 3 columns, header has 2
		           f"+916100000082,{NAME_PREFIX}R82\n")
		resp = self._submit_file("lead_create", "csv", content)
		partner_bulk_worker.process_job(resp["data"]["job_id"])
		frappe.set_user("Administrator")
		job = frappe.get_doc("CRM Bulk Job", resp["data"]["job_id"])
		self.assertEqual((job.status, job.succeeded, job.failed), ("JobComplete", 2, 1))
		fail = frappe.get_all("CRM Bulk Job Result", filters={"job": job.name, "action": "failed"},
		                      fields=["record_index"])
		self.assertEqual([f.record_index for f in fail], [1])

	def test_virus_file_is_failed_and_purged(self):
		eicar = r"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
		frappe.db.set_value("CRM Tatva Automation", "Storage::File::screening", "enabled", 1)
		settings = frappe.get_single("CRM File Screening Settings")
		if "Partner API" not in {r.channel for r in settings.active_channels}:
			settings.append("active_channels", {"channel": "Partner API"})
			settings.save(ignore_permissions=True)
		frappe.db.commit()
		try:
			resp = self._submit_file("lead_create", "jsonl", eicar)
			job_id = resp["data"]["job_id"]
			partner_bulk_worker.process_job(job_id)
			frappe.set_user("Administrator")
			job = frappe.get_doc("CRM Bulk Job", job_id)
			self.assertEqual((job.status, job.succeeded), ("Failed", 0))  # blocked, nothing parsed
			self.assertFalse(frappe.db.exists(
				"File", {"attached_to_doctype": "CRM Bulk Job", "attached_to_name": job_id}))  # purged
		finally:
			frappe.db.set_value("CRM Tatva Automation", "Storage::File::screening", "enabled", 0)
			frappe.db.commit()


	# -- Phase 3: native webhook + SSRF guard ---------------------------------

	def test_terminal_job_fires_the_partner_webhook(self):
		"""A job reaching a terminal state saves through the ORM, so Frappe's native Webhook fires for
		the partner. We capture the native enqueue (never a real POST); the outcome is that the pipeline
		fired for THIS job, in its terminal status, matching the per-partner condition."""
		frappe.set_user("Administrator")
		with patch("tatva_connect.utils.assert_safe_public_url"):  # off so creation needs no DNS
			wh = frappe.get_doc({
				"doctype": "Webhook", "name": "Bulk Job Done (test)",  # Webhook autoname is prompt
				"webhook_doctype": "CRM Bulk Job", "webhook_docevent": "on_update",
				"request_url": "https://partner.example.test/bulk-done", "request_method": "POST",
				"condition": f"doc.status in ['JobComplete','Failed','Aborted'] and doc.partner == '{PARTNER}'",
			}).insert(ignore_permissions=True)
		try:
			job_id = self._submit("lead_create", [self._lead(90, "W")])["data"]["job_id"]
			enq = []
			with patch("frappe.enqueue", side_effect=lambda *a, **k: enq.append((a, k))):
				partner_bulk_worker.process_job(job_id)  # finish_job's commit flushes the webhook queue
			frappe.set_user("Administrator")
			hook_calls = [k for a, k in enq if a and "enqueue_webhook" in str(a[0])]
			self.assertTrue(hook_calls, "the terminal transition did not enqueue the native webhook")
			self.assertEqual(hook_calls[0]["doc"].name, job_id)
			self.assertEqual(hook_calls[0]["doc"].status, "JobComplete")
		finally:
			frappe.set_user("Administrator")
			frappe.delete_doc("Webhook", wh.name, force=True, ignore_permissions=True)
			frappe.client_cache.delete_value("webhooks")
			frappe.db.commit()

	def test_bulk_job_webhook_url_is_ssrf_guarded(self):
		"""A completion-webhook URL may not resolve to an internal/metadata address; the guard is scoped
		to CRM Bulk Job webhooks so no unrelated webhook is touched."""
		frappe.set_user("Administrator")
		with self.assertRaisesRegex(frappe.ValidationError, "unsafe URL"):
			frappe.get_doc({
				"doctype": "Webhook", "name": "Bulk Job SSRF (test)",  # Webhook autoname is prompt
				"webhook_doctype": "CRM Bulk Job", "webhook_docevent": "on_update",
				"request_url": "http://169.254.169.254/latest/meta-data/", "request_method": "POST",
			}).insert(ignore_permissions=True)
		from types import SimpleNamespace  # scoped: the same internal URL on another doctype is not blocked
		partner_bulk_job.guard_webhook_url(
			SimpleNamespace(webhook_doctype="ToDo", request_url="http://169.254.169.254/x"))

	def test_failed_job_fires_webhook_even_with_a_lead_webhook_queued(self):
		"""B1 regression: a CRM Lead webhook queues on the chunk's lead insert; a mid-drain failure rolls
		back (resetting after_commit). finish_job MUST still enqueue the Failed webhook — the worker must
		drop the stale queue on rollback so the flush re-registers. Red before the _rollback fix."""
		frappe.set_user("Administrator")
		with patch("tatva_connect.utils.assert_safe_public_url"):
			lead_wh = frappe.get_doc({
				"doctype": "Webhook", "name": "Lead Insert (test)",
				"webhook_doctype": "CRM Lead", "webhook_docevent": "after_insert",
				"request_url": "https://lead.example.test/hook", "request_method": "POST",
			}).insert(ignore_permissions=True)
			job_wh = frappe.get_doc({
				"doctype": "Webhook", "name": "Bulk Job Fail (test)",
				"webhook_doctype": "CRM Bulk Job", "webhook_docevent": "on_update",
				"request_url": "https://partner.example.test/bulk-done", "request_method": "POST",
				"condition": f"doc.status == 'Failed' and doc.partner == '{PARTNER}'",
			}).insert(ignore_permissions=True)
		try:
			job_id = self._submit("lead_create", [self._lead(95, "F")])["data"]["job_id"]
			enq = []
			# fail AFTER the chunk's lead is created, so its after_insert webhook is already queued
			with patch("tatva_connect.api.partner_bulk_worker._write_results",
			           side_effect=RuntimeError("boom")), \
			     patch("frappe.enqueue", side_effect=lambda *a, **k: enq.append((a, k))):
				partner_bulk_worker.process_job(job_id)
			frappe.set_user("Administrator")
			self.assertEqual(frappe.db.get_value("CRM Bulk Job", job_id, "status"), "Failed")
			hook_calls = [k for a, k in enq if a and "enqueue_webhook" in str(a[0])]
			self.assertTrue(any(k["doc"].name == job_id for k in hook_calls),
			                "the Failed completion webhook was dropped (stale webhook queue after rollback)")
		finally:
			frappe.set_user("Administrator")
			for wh in (lead_wh.name, job_wh.name):
				frappe.delete_doc("Webhook", wh, force=True, ignore_permissions=True)
			frappe.client_cache.delete_value("webhooks")
			frappe.db.commit()


	# -- Phase 4: cancel-race writer election ---------------------------------

	def test_guarded_abort_wins_before_the_worker_starts(self):
		"""A pre-start cancel wins the atomic election from UploadComplete and drops the job to Aborted."""
		frappe.set_user("Administrator")
		job_id = self._submit("lead_create", [self._lead(88, "E")])["data"]["job_id"]
		won = partner_bulk_worker.finish_job(job_id, "Aborted", commit=False, guard_pre_start=True)
		frappe.db.commit()
		self.assertTrue(won)
		self.assertEqual(frappe.db.get_value("CRM Bulk Job", job_id, "status"), "Aborted")

	def test_guarded_abort_loses_to_a_started_worker_and_never_clobbers(self):
		"""Once a job is InProgress (the worker claimed it), a pre-start abort matches zero rows: finish_job
		returns False and does NOT clobber the status — this is the election that closes the cancel race."""
		frappe.set_user("Administrator")
		job_id = self._submit("lead_create", [self._lead(89, "E")])["data"]["job_id"]
		frappe.db.set_value("CRM Bulk Job", job_id, "status", "InProgress")  # worker already claimed it
		frappe.db.commit()
		won = partner_bulk_worker.finish_job(job_id, "Aborted", commit=False, guard_pre_start=True)
		frappe.db.commit()
		self.assertFalse(won)  # the abort lost the election
		self.assertEqual(frappe.db.get_value("CRM Bulk Job", job_id, "status"), "InProgress")  # untouched

	def test_cancel_of_a_started_job_is_cooperative(self):
		"""The cancel endpoint on a job the worker already owns falls through to the cooperative channel
		(cancel_requested) the worker polls — never a status clobber."""
		frappe.set_user("Administrator")
		job_id = self._submit("lead_create", [self._lead(87, "E")])["data"]["job_id"]
		frappe.db.set_value("CRM Bulk Job", job_id, "status", "InProgress")
		frappe.db.commit()
		frappe.set_user(PARTNER)
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(job_id=job_id)
		partner_bulk_job.cancel()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.local.response["data"]["status"], "InProgress")
		self.assertEqual(frappe.db.get_value("CRM Bulk Job", job_id, "cancel_requested"), 1)


	# -- Phase 4: reaper + retention purge ------------------------------------

	def test_reaper_fails_a_stranded_job_but_spares_a_recent_one(self):
		"""A job InProgress past the job timeout (its worker died) is reaped -> Failed + payload purged;
		a job InProgress within the window is left alone."""
		frappe.set_user("Administrator")
		content = "\n".join(json.dumps(self._lead(80 + i, "S")) for i in range(2))
		dead = self._submit_file("lead_create", "jsonl", content)["data"]["job_id"]
		alive = self._submit_file("lead_create", "jsonl", content)["data"]["job_id"]
		frappe.set_user("Administrator")
		old = add_to_date(now_datetime(), seconds=-(3600 + 120))  # past the 3600s default timeout
		frappe.db.set_value("CRM Bulk Job", dead, {"status": "InProgress", "started_at": old}, update_modified=False)
		frappe.db.set_value("CRM Bulk Job", alive, {"status": "InProgress", "started_at": now_datetime()}, update_modified=False)
		frappe.db.set_value("CRM Tatva Automation", "Partner::AsyncBulk::reaper", "enabled", 1)
		frappe.db.commit()
		try:
			partner_bulk_worker.reap_stranded_jobs()
			frappe.db.commit()
			self.assertEqual(frappe.db.get_value("CRM Bulk Job", dead, "status"), "Failed")
			self.assertFalse(frappe.db.exists(
				"File", {"attached_to_doctype": "CRM Bulk Job", "attached_to_name": dead}))
			self.assertEqual(frappe.db.get_value("CRM Bulk Job", alive, "status"), "InProgress")  # spared
		finally:
			frappe.db.set_value("CRM Tatva Automation", "Partner::AsyncBulk::reaper", "enabled", 0)
			frappe.db.commit()

	def test_purge_removes_finished_jobs_past_retention_but_keeps_recent(self):
		"""A finished job past the retention window is purged with its results + payload; a recent one stays."""
		frappe.set_user("Administrator")
		old_id = self._submit("lead_create", [self._lead(75, "P")])["data"]["job_id"]
		partner_bulk_worker.process_job(old_id)
		recent_id = self._submit("lead_create", [self._lead(76, "P")])["data"]["job_id"]
		partner_bulk_worker.process_job(recent_id)
		frappe.set_user("Administrator")
		stale = add_to_date(now_datetime(), days=-(7 + 1))  # past the 7-day default retention
		frappe.db.set_value("CRM Bulk Job", old_id, "finished_at", stale, update_modified=False)
		frappe.db.set_value("CRM Tatva Automation", "Partner::AsyncBulk::purge", "enabled", 1)
		frappe.db.commit()
		try:
			partner_bulk_job.purge_expired_jobs()
			frappe.db.commit()
			self.assertFalse(frappe.db.exists("CRM Bulk Job", old_id))
			self.assertEqual(frappe.db.count("CRM Bulk Job Result", {"job": old_id}), 0)  # results cascaded
			self.assertFalse(frappe.db.exists(
				"File", {"attached_to_doctype": "CRM Bulk Job", "attached_to_name": old_id}))
			self.assertTrue(frappe.db.exists("CRM Bulk Job", recent_id))  # within retention, kept
		finally:
			frappe.db.set_value("CRM Tatva Automation", "Partner::AsyncBulk::purge", "enabled", 0)
			frappe.db.commit()


	def test_enqueue_id_kwarg_reaches_process_job_and_is_not_reserved(self):
		"""The kwarg create passes to frappe.enqueue to carry the job id must (a) not collide with a
		RESERVED enqueue parameter (job_id/job_name/timeout/…, which enqueue would swallow) and (b) match
		process_job's parameter. Guards the enqueue→worker seam the in-process tests never exercise."""
		import inspect

		from frappe.utils import background_jobs
		frappe.set_user(PARTNER)
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(operation="lead_create", format="inline", records=[self._lead(70, "Q")])
		calls = []
		with patch("frappe.enqueue", side_effect=lambda *a, **k: calls.append((a, k))):
			partner_bulk_job.create()
		frappe.set_user("Administrator")
		# isolate the process_job enqueue (create also enqueues the File's Azure offload)
		proc = [k for a, k in calls if a and "process_job" in str(a[0])]
		self.assertEqual(len(proc), 1, "create must enqueue process_job exactly once")
		reserved = set(inspect.signature(background_jobs.enqueue).parameters) - {"kwargs"}
		passthrough = [k for k in proc[0] if k not in reserved]
		self.assertEqual(len(passthrough), 1, f"expected exactly one non-reserved id kwarg, got {passthrough}")
		self.assertIn(passthrough[0], inspect.signature(partner_bulk_worker.process_job).parameters)

	def _in_flight(self, count):
		"""`count` non-terminal jobs for PARTNER, so the caps have something to push back against."""
		for _ in range(count):
			frappe.get_doc({"doctype": "CRM Bulk Job", "partner": PARTNER, "operation": "lead_create",
			                "input_format": "inline", "status": "UploadComplete"}
			               ).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture

	def test_an_idle_queue_pushes_back_on_nothing(self):
		frappe.set_user(PARTNER)
		_user, mp, _is = partner_bulk_job._resolve_caller()
		self.assertIsNone(partner_bulk_job.queue_pressure(PARTNER, mp))

	def test_the_per_partner_cap_refuses_a_further_submit_with_429(self):
		"""The caller's OWN cap is a 429; it must apply wherever a job is submitted, not just over HTTP."""
		frappe.set_user(PARTNER)
		_user, mp, _is = partner_bulk_job._resolve_caller()
		frappe.set_user("Administrator")
		self._in_flight(partner_bulk_job._cfg()["async_concurrent_jobs_per_partner"])
		pressure = partner_bulk_job.queue_pressure(PARTNER, mp)
		self.assertIsNotNone(pressure, "a full per-partner queue accepted another job")
		self.assertEqual(pressure[0], "rate_limited")
		self.assertEqual(pressure[2], 429)

	def test_submit_job_owns_its_payload_and_enqueues_the_worker_once(self):
		"""The ONE submit path: a job row, its own attached payload, one process_job enqueue."""
		frappe.set_user("Administrator")
		calls = []
		with patch("frappe.enqueue", side_effect=lambda *a, **k: calls.append((a, k))):
			name = partner_bulk_job.submit_job(PARTNER, "lead_create", "csv", b"mobile_no\n+916100009999\n")
		job = frappe.get_doc("CRM Bulk Job", name)
		self.assertEqual((job.partner, job.input_format, job.status), (PARTNER, "csv", "UploadComplete"))
		self.assertTrue(frappe.db.exists("File", {"attached_to_doctype": "CRM Bulk Job",
		                                          "attached_to_name": name}), "the payload is not owned by the job")
		proc = [k for a, k in calls if a and "process_job" in str(a[0])]
		self.assertEqual(len(proc), 1, "submit_job must enqueue process_job exactly once")
		self.assertEqual(proc[0].get("queue"), "partner_bulk")

	def test_submit_job_carries_extra_columns_onto_the_job(self):
		"""A Desk import names itself on the job through `extra`, without a second creator existing."""
		frappe.set_user("Administrator")
		with patch("frappe.enqueue"):
			name = partner_bulk_job.submit_job(PARTNER, "lead_create", "csv", b"mobile_no\n+916100009998\n",
			                                   extra={"error_summary": "carried"})
		self.assertEqual(frappe.db.get_value("CRM Bulk Job", name, "error_summary"), "carried")


if __name__ == "__main__":
	unittest.main()
