# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What a partner is TOLD must not depend on which lane answered.

The static lock (`tests/static/test_partner_message_quality.py`) catches the four things a message may
never say. These are the OUTCOMES it cannot see: that a per-record failure carries the same structure
whether it ran in a request or in a worker, that a crashed job says something a partner can act on, and
that one condition reads with one voice in every lane.

Every assertion here reads a response body or a stored row. None asserts that a function was called.
"""
import json
import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import _base, partner, partner_bulk_job, partner_bulk_worker, partner_file

PARTNER = "errparity.lock.partner@example.test"
VERTICAL, GROUP = "GoodFlip Care", "Anaya"
TOGGLE = "Partner::AsyncBulk::jobs"
NAME_PREFIX = "ErrParity"

# The limiter is not what this suite reads, and left live it answers FIRST: the bulk bucket has a
# capacity of 1, so the second bulk call in a run came back `Bulk rate limit exceeded` instead of the
# refusal under test. Same patch target `test_partner_limiter` uses — it rebinds the name inside _base
# only, so partner_bulk_job's own `Partner::AsyncBulk::jobs` toggle is still read from the database.
_ENFORCE = "tatva_connect.api._base.automation.is_enabled"

# The same leak the static lock refuses in source, asserted here against what actually reached the wire.
_CLASS_NAME = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:Error|Exception)\b")


def _mint_partner(email):
	if not frappe.db.exists("User", email):
		frappe.get_doc({"doctype": "User", "email": email, "first_name": "Err Parity",
		                "send_welcome_email": 0, "user_type": "System User"}).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	if "Partner API User" not in [r.role for r in user.roles]:
		user.append("roles", {"role": "Partner API User"})
		user.save(ignore_permissions=True)
	if not frappe.db.exists("CRM Lead API Mapping", {"partner_user": email}):
		frappe.get_doc({"doctype": "CRM Lead API Mapping", "partner_user": email, "enabled": 1,
		                "contract_name": email, "vertical": VERTICAL,
		                "crm_group": GROUP}).insert(ignore_permissions=True)


def _purge_test_leads():
	for name in frappe.get_all("CRM Lead", filters={"first_name": ["like", f"{NAME_PREFIX}%"]}, pluck="name"):
		frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)


class TestPartnerErrorParity(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_mint_partner(PARTNER)
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1)
		_purge_test_leads()
		frappe.db.commit()  # survives the per-test rollback; the worker commits as it drains

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_purge_test_leads()
		for job in frappe.get_all("CRM Bulk Job", filters={"partner": PARTNER}, pluck="name"):
			frappe.delete_doc("CRM Bulk Job", job, force=True, ignore_permissions=True)
		for contract in frappe.get_all("CRM Lead API Mapping", filters={"partner_user": PARTNER}, pluck="name"):
			frappe.delete_doc("CRM Lead API Mapping", contract, force=True, ignore_permissions=True)
		if frappe.db.exists("User", PARTNER):
			frappe.delete_doc("User", PARTNER, force=True, ignore_permissions=True)
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 0)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user(PARTNER)
		frappe.local.response = frappe._dict()
		enforcement = patch(_ENFORCE, return_value=False)
		enforcement.start()
		self.addCleanup(enforcement.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		for job in frappe.get_all("CRM Bulk Job", filters={"partner": PARTNER}, pluck="name"):
			frappe.delete_doc("CRM Bulk Job", job, force=True, ignore_permissions=True)
		_purge_test_leads()
		frappe.db.commit()

	# The one bad record both lanes are driven with: a lead carrying no phone number, which is the
	# identity field — so `mobile_no` is the named input and the refusal must say so, in both lanes.
	_BAD_LEAD = {"first_name": f"{NAME_PREFIX}NoPhone"}

	def _sync_bulk_error(self):
		frappe.set_user(PARTNER)
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(leads=[dict(self._BAD_LEAD)])
		partner.lead_create_bulk()
		return frappe.local.response["results"][0]["error"]

	def _job_result_row(self):
		frappe.set_user(PARTNER)
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(operation="lead_create", format="inline",
		                                records=[dict(self._BAD_LEAD)])
		with patch("frappe.enqueue"):
			partner_bulk_job.create()
		job_id = frappe.local.response["data"]["job_id"]
		partner_bulk_worker.process_job(job_id)
		frappe.set_user(PARTNER)
		return job_id, frappe.db.get_value(
			"CRM Bulk Job Result", {"job": job_id, "action": "failed"},
			["error_code", "error_message", "error_fields", "error_detail"], as_dict=True,
		)

	def test_a_jobs_per_record_failure_carries_the_same_error_object_as_its_sync_twin(self):
		"""_write_results wrote only code + message, so the SAME refusal arrived with less information
		purely because it ran async — and a client branching on `error.fields` could branch in one lane
		and not the other."""
		sync = self._sync_bulk_error()
		self.assertEqual(sync["fields"], ["mobile_no"],
		                 "the sync lane must name the input that was at fault")

		_job_id, row = self._job_result_row()
		self.assertIsNotNone(row, "the job must record its failed record")
		self.assertEqual(row.error_code, sync["code"])
		self.assertEqual(row.error_message, sync["message"])
		self.assertEqual(json.loads(row.error_fields), sync["fields"],
		                 "the async lane must carry the SAME error.fields as its sync twin")

	def test_the_results_read_path_hands_fields_back_as_json_not_as_text(self):
		"""Storing the array is half the job: a partner reads it through partner_bulk_job.results, and a
		client cannot be asked to parse a string in one lane and a list in the other."""
		job_id, _row = self._job_result_row()
		frappe.set_user(PARTNER)
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(job_id=job_id)
		partner_bulk_job.results()
		row = frappe.local.response["data"]["results"][0]
		self.assertEqual(row["error_fields"], ["mobile_no"])
		self.assertNotIsInstance(row["error_fields"], str, "fields must read as a JSON array, not text")

	def test_a_crashed_job_reports_a_code_and_a_sentence_not_a_class_name(self):
		"""error_summary is the ONLY thing a partner gets for a mid-drain crash, and it shipped
		`RuntimeError: boom`."""
		frappe.set_user(PARTNER)
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(operation="lead_create", format="inline",
		                                records=[{"mobile_no": "+919760000001",
		                                          "first_name": f"{NAME_PREFIX}Crash"}])
		with patch("frappe.enqueue"):
			partner_bulk_job.create()
		job_id = frappe.local.response["data"]["job_id"]

		with patch.object(partner_bulk_worker, "_process_bulk",
		                  side_effect=RuntimeError("clamd socket died")):
			partner_bulk_worker.process_job(job_id)

		frappe.set_user("Administrator")
		job = frappe.get_doc("CRM Bulk Job", job_id)
		self.assertEqual(job.status, "Failed")
		summary = job.error_summary or ""
		self.assertNotRegex(summary, _CLASS_NAME,
		                    f"a Python class name reached the partner: {summary!r}")
		self.assertNotIn("clamd socket died", summary,
		                 "the raw exception text is our internals, not the caller's next step")
		# A code a partner can look up, and a sentence that says what to do with the job.
		self.assertTrue(any(code in summary for code in _base.ERROR_CODES),
		                f"error_summary names no declared error code: {summary!r}")
		self.assertIn("partner_bulk_job.results", summary,
		              "a crashed job must point at where its partial outcome is readable")

	def test_bad_base64_reads_identically_in_the_single_and_the_job_lane(self):
		"""One condition, one voice. The single attach and the job submit both refuse undecodable
		base64; they used to do it with two different sentences, one carrying `fields` and one not."""
		bad = "!!!not base64!!!"
		frappe.set_user(PARTNER)

		frappe.form_dict = frappe._dict(content_base64=bad)
		with self.assertRaises(frappe.ValidationError) as job_lane:
			partner_bulk_job._uploaded_bytes(dict(_base.DEFAULTS))

		with self.assertRaises(frappe.ValidationError) as single_lane:
			partner_file._load_bytes({"content_base64": bad})

		self.assertEqual(str(single_lane.exception), str(job_lane.exception),
		                 "the same condition must read the same in both lanes")
		self.assertEqual(str(single_lane.exception), _base.base64_message())
		self.assertEqual(single_lane.exception.fields, ["content_base64"],
		                 "the single lane names the input at fault")
		self.assertEqual(job_lane.exception.fields, ["content_base64"],
		                 "the job lane must name it too — it used to name nothing")

	def test_a_json_array_argument_sent_as_text_reads_as_a_sentence_not_as_parser_text(self):
		"""`{"leads": "not-a-list"}` answered `invalid literal: line 1 column 1 (char 0)` — orjson's own
		text, about OUR read of the bytes, carrying a blame word, as the WHOLE partner-facing message.

		The static lock cannot see this one: the prose is interpolated at runtime from an exception the
		partner modules never wrote, so there is no literal to scan. It is caught here or nowhere."""
		lanes = (
			("sync bulk", partner.lead_create_bulk, {"leads": "not-a-list"}, "leads"),
			("async job", partner_bulk_job.create,
			 {"operation": "lead_create", "format": "inline", "records": "not-a-list"}, "records"),
		)
		for lane, endpoint, form, key in lanes:
			with self.subTest(lane=lane):
				frappe.set_user(PARTNER)
				frappe.local.response = frappe._dict()
				frappe.form_dict = frappe._dict(form)
				endpoint()
				error = frappe.local.response["error"]
				self.assertEqual(error["code"], "validation_error")
				self.assertEqual(error["fields"], [key],
				                 "the refusal must name the argument at fault in error.fields")
				self.assertNotRegex(error["message"], r"line \d+ column \d+",
				                    f"the parser's own text reached the partner: {error['message']!r}")
				self.assertNotRegex(error["message"], r"(?i)\b(invalid|incorrect|illegal)\b",
				                    "a refusal never blames the caller")
				self.assertIn(f"`{key}`", error["message"],
				              "the message names the argument for a human reading it")

	def test_the_record_cap_reads_identically_in_every_lane(self):
		"""Three lanes enforced one rule with three sentences (`Page the rest.` / `Max N per inline
		job` / `Exceeds the N record limit.`). They now share one producer, so the caller reads one
		rule whichever lane refused."""
		frappe.set_user(PARTNER)
		frappe.local.response = frappe._dict()
		ceiling = _base.bulk_max()
		frappe.form_dict = frappe._dict(leads=[dict(self._BAD_LEAD) for _ in range(ceiling + 1)])
		partner.lead_create_bulk()
		sync_message = frappe.local.response["error"]["message"]
		self.assertEqual(sync_message, _base.record_cap_message(ceiling, ceiling + 1, frappe._("call")))

		# the worker's file-payload lane, driven with a cap it really exceeds
		small = dict(_base.DEFAULTS, async_file_max_records=2)
		job = frappe._dict(name="unsaved", input_format="jsonl")
		with patch.object(partner_bulk_worker, "_cfg", return_value=small):
			with self.assertRaises(partner_bulk_worker.PayloadRejected) as rejected:
				partner_bulk_worker._to_batch(job, b'{"a":1}\n{"a":2}\n{"a":3}\n')
		self.assertEqual(str(rejected.exception),
		                 _base.record_cap_message(2, 4, frappe._("file payload")),
		                 "the worker must refuse with the SAME rule the request lane refuses with")
