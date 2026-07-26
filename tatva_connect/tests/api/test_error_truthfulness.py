# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An error must say WHOSE fault it was, WHICH input caused it, and WHAT was decided.

Four lies the API told before this module existed, each pinned below:

  1. Our scanner being down came back as a 400 "invalid file" — blaming the partner for our outage,
     and telling a well-built client to stop retrying the one call that would have succeeded.
  2. The screening verdict (Type Not Allowed / Type Mismatch / Infected / Scanner Unavailable) was
     recorded richly in CRM File Scan Log and flattened to one English sentence everywhere a caller
     or an operator could actually see it.
  3. Nine refusals in the file API named the offending field in prose only, so a client had to parse
     English to learn which key it sent wrong — while `error.fields` existed and went unused.
  4. A bulk call where every record failed answered 200 and was FILED as a clean success.

The caller here is a trusted System Manager: this pins the error CONTRACT, not the grain gate.
"""
import unittest
from unittest.mock import patch

import frappe
import requests

from tatva_connect.api import _base, partner_file
from tatva_connect.storage import file_screening

CLEAN_PDF = b"%PDF-1.4 clean"


def screen_under_stubbed_config(*, verdict_scan=("clean", ""), file_type="PDF", raw=CLEAN_PDF,
	allowed=True):
	"""Run the real `screen()` with the OPERATOR config stubbed and nothing else.

	The toggle, the active-channel list and the extension list are operator data, not behaviour: a
	test that flips them on the site would leave a live config change behind (and the scan-log enqueue
	is silenced for the same reason — a queued row would outlive the rollback). The verdict logic, the
	block policy, `_block_exception` and the throw are all real.

	Module-level so the observability suite drives the SAME producer this one does — a second copy of
	the fixture is how one suite ends up locking a shape the other never produces."""
	with patch.object(file_screening.automation, "is_enabled", return_value=True), \
		patch.object(file_screening, "_cfg", side_effect=file_screening.DEFAULTS.get), \
		patch.object(file_screening, "_active_channels", return_value={file_screening._PARTNER}), \
		patch.object(file_screening, "_allowed", return_value=allowed), \
		patch.object(file_screening, "_scan", return_value=verdict_scan), \
		patch.object(file_screening, "_log_scan"):
		file_screening.screen(
			file_name="report.pdf", file_type=file_type, raw=raw,
			attached_to_doctype="CRM Lead", attached_to_name="does-not-matter",
		)


class TestErrorTruthfulness(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		self.sp = f"err_truth_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)
		self._form = frappe.form_dict
		self._response = frappe.local.response
		self._headers = frappe.local.response_headers
		frappe.form_dict = frappe._dict()
		frappe.local.response = frappe._dict()
		frappe.local.response_headers = frappe._dict()
		frappe.local.partner_bulk_error = None

	def tearDown(self):
		frappe.form_dict = self._form
		# Restore, never just overwrite: these ARE the live request envelope, and a test that leaves
		# its own _dict behind hands the next one a response and headers it never wrote.
		frappe.local.response = self._response
		frappe.local.response_headers = self._headers
		frappe.local.partner_bulk_error = None
		try:
			frappe.db.rollback(save_point=self.sp)
		except Exception:
			frappe.db.rollback()

	# -- helpers -------------------------------------------------------------

	def _drive(self, fn):
		"""Run `fn` through the REAL @_api wrapper and hand back the envelope it wrote — the same
		object build_response serialises, so this is the partner's own view of the failure."""
		frappe.local.response = frappe._dict()
		_base._api(fn)()
		return frappe.local.response

	def _a_lead(self):
		"""A real CRM Lead to resolve against. Created, never looked up and skipped over: a test that
		goes green because the site happened to be empty locks nothing at all.

		Inserted with ignore_permissions, which is the trusted-server path every partner write already
		takes — `stamp_entitled_grain` returns immediately on that flag, so this is the same quiet
		insert `lead_create` performs and not a second way of making a lead. No `mobile_no`: it is the
		dedup anchor and this lead has no business colliding with a real one (`dedup_guard` returns
		early without it)."""
		status = frappe.db.get_value("CRM Lead Status", {"type": "Open"}, "name")
		self.assertTrue(status, "a CRM Lead needs an OPEN status; a Lost one demands a lost reason")
		lead = frappe.get_doc({
			"doctype": "CRM Lead",
			"first_name": "Error Truthfulness",
			"status": status,
		})
		lead.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, rolled back on teardown
		return lead.name

	# -- 2.1: whose fault was it ---------------------------------------------

	def test_a_scanner_outage_is_ours_and_a_bad_file_is_theirs(self):
		"""Every blocked verdict threw ValidationError, so ClamAV being down reached the partner as a
		400 "This file type is not accepted"-class refusal. Under the fail-closed policy the file was
		never judged at all: that is a 503 (frappe.ServiceUnavailableError, http_status_code = 503),
		and the client faults must stay 400 or the fix has merely moved the lie."""
		cases = (
			("Scanner Unavailable", dict(verdict_scan=("unavailable", "")), frappe.ServiceUnavailableError),
			("Infected", dict(verdict_scan=("infected", "Eicar-Test")), frappe.ValidationError),
			("Type Not Allowed", dict(allowed=False), frappe.ValidationError),
			("Type Mismatch", dict(raw=b"<html>not a pdf"), frappe.ValidationError),
		)
		for verdict, kwargs, expected in cases:
			with self.subTest(verdict=verdict):
				frappe.local.partner_ctx = ("Administrator", None, True)
				try:
					with self.assertRaises(expected):
						screen_under_stubbed_config(**kwargs)
				finally:
					frappe.local.partner_ctx = None

	def test_the_outage_reaches_the_partner_as_a_retryable_server_busy(self):
		"""_ERROR_MAP had no entry for the native 503, so it fell through to the generic branch and the
		partner got an opaque 500 with no retry guidance. `server_busy` is the EXISTING code for exactly
		this ("the service is at capacity; not the caller's fault, nothing spent") — the vocabulary is
		closed and no new code is invented."""
		def boom(**_kwargs):
			raise frappe.ServiceUnavailableError("File could not be security-scanned.")

		resp = self._drive(boom)

		self.assertEqual(resp["error"]["code"], "server_busy")
		self.assertEqual(resp["http_status_code"], 503)

	def test_an_outage_of_unknown_length_does_not_invent_a_retry_after(self):
		"""The first fix for the above handed the partner `bulk_window_seconds` — the BULK RATE-LIMIT
		window — as the recovery time for a ClamAV outage. Two unrelated quantities, and it made the
		docs' promise ("it is a real number, not an estimate") false. Nobody knows when a scanner comes
		back, so the honest answer is no number at all and exponential backoff, in body AND header."""
		def boom(**_kwargs):
			raise frappe.ServiceUnavailableError("File could not be security-scanned.")

		resp = self._drive(boom)

		self.assertNotIn("retry_after", resp["error"],
		                 "a number nobody measured is worse than no number")
		self.assertIsNone(frappe.local.response_headers.get("Retry-After"),
		                  "the header must not reinstate what the body refused to claim")

	def test_every_mapped_exception_classifies_as_its_own_entry(self):
		"""_ERROR_MAP was walked by isinstance() in INSERTION order, so the broad ValidationError entry
		(third) answered for every frappe exception deriving from it — a rate limit came back 400 "your
		body was invalid", and each later registration was dead on arrival. The lookup is by MRO now, so
		this asserts the property rather than a list, and covers whatever the map grows next."""
		for exc_type, (code, http) in _base._ERROR_MAP.items():
			with self.subTest(exc=exc_type.__name__):
				got_code, got_http, _m, _f, _d = _base._classify(exc_type("probe"), "probe")
				self.assertEqual(
					(got_code, got_http), (code, http),
					f"{exc_type.__name__} must classify as its OWN entry, never an ancestor's",
				)

	def test_the_failure_path_degrades_on_an_undeclared_code_instead_of_detonating(self):
		"""`checked_code` raised in developer_mode — from inside `_fail`, which `@_api` calls in its own
		`except`. The raise escaped the wrapper, so the partner got a bare traceback instead of the
		envelope AND `_idempotency_release` (the line after `_fail`) never ran: the claim sat `pending`
		and every retry answered 409 until it went stale. `_classify` is the seam, because by
		construction no real exception can produce a code the closed vocabulary does not carry."""
		released = []
		with patch.dict(frappe.conf, {"developer_mode": 1}), \
			patch.object(frappe, "log_error"), \
			patch.object(_base, "_idem_key", return_value="K"), \
			patch.object(_base, "_idempotency_begin", return_value=("run", "CLAIM")), \
			patch.object(_base, "_idempotency_release", side_effect=released.append), \
			patch.object(_base, "_classify", return_value=("not_a_declared_code", 500, "boom", None, None)):

			def boom(**_kwargs):
				frappe.throw("anything")

			resp = self._drive(boom)

		self.assertEqual(resp["error"]["code"], "server_error", "an undeclared code degrades, it does not escape")
		self.assertEqual(resp["http_status_code"], 500, "the caller still gets the envelope, not a traceback")
		self.assertEqual(released, ["CLAIM"], "a failed write must ALWAYS release its idempotency claim")

	# -- 2.2: what was decided -----------------------------------------------

	def test_a_blocked_upload_names_its_verdict_in_the_error_envelope(self):
		"""The verdict was written richly to CRM File Scan Log and nowhere a caller could see it: the
		partner got one sentence, joinable to the real answer only by a manual trace_id lookup. It rides
		on the exception (frappe.throw carries no structure) and _classify lifts it onto `error.detail`."""
		def attach(**_kwargs):
			screen_under_stubbed_config(verdict_scan=("infected", "Eicar-Test-Signature"))

		resp = self._drive(attach)

		detail = resp["error"].get("detail")
		self.assertIsNotNone(detail, "the envelope must carry the structured verdict, not just prose")
		self.assertEqual(detail["verdict"], "Infected")
		self.assertEqual(detail["signature"], "Eicar-Test-Signature")
		self.assertEqual(detail["file_name"], "report.pdf")
		self.assertEqual(detail["check"], "file_screening")

	# -- 2.3: which input was wrong ------------------------------------------

	def test_every_file_refusal_names_the_field_it_is_about(self):
		"""All NINE throws in partner_file, driven — not four of them with a docstring claiming nine.

		`error.fields` is the mechanism the rest of the API already uses (`_base.throw_field`, which
		this module's nine now share) and `_api` already forwards it; it was simply never applied here.
		The three `file_url` refusals need a download to fail in a specific way, so `requests.get` and
		the SSRF pre-check are stubbed — the refusal, the field list and the class are all real."""
		big = b"x" * (2 * 1024 * 1024)
		small_cfg = {"file_download_max_mb": 1, "file_download_timeout_seconds": 5}

		def a_response(status_code=200, chunks=(b"%PDF-1.4",)):
			return frappe._dict(
				status_code=status_code,
				raise_for_status=lambda: None,
				iter_content=lambda _size: iter(chunks),
			)

		cases = (
			("no bytes at all", lambda: partner_file._load_bytes({}), ["file_url", "content_base64"]),
			("bad base64", lambda: partner_file._load_bytes({"content_base64": "!!!not-base64!!!"}),
			 ["content_base64"]),
			("no file id", lambda: partner_file._scoped_file(None, None, True), ["name"]),
			("unknown file id", lambda: partner_file._scoped_file("no-such-file-id", None, True), ["name"]),
			("activity on another lead",
			 lambda: partner_file._resolve_target({"activity": "no-such-task"}, "no-such-lead"), ["activity"]),
			("note on another lead",
			 lambda: partner_file._resolve_target({"note": "no-such-note"}, "no-such-lead"), ["note"]),
		)
		for label, run, expected in cases:
			with self.subTest(case=label):
				resp = self._drive(lambda **_kwargs: run())
				self.assertEqual(
					resp["error"].get("fields"), expected,
					f"{label}: the refusal must name the input, not describe it",
				)

		download_cases = (
			("file_url redirects", a_response(status_code=302), None),
			("file_url exceeds the size cap", a_response(chunks=(big, big)), None),
			("file_url will not fetch", None, requests.exceptions.ConnectionError("refused")),
		)
		for label, response, error in download_cases:
			with self.subTest(case=label):
				with patch("tatva_connect.utils.assert_safe_public_url"), \
					patch.object(partner_file, "_cfg", return_value=small_cfg), \
					patch.object(requests, "get", return_value=response, side_effect=error):
					resp = self._drive(
						lambda **_kwargs: partner_file._load_bytes({"file_url": "https://example.com/a.pdf"})
					)
				self.assertEqual(
					resp["error"].get("fields"), ["file_url"],
					f"{label}: the refusal must name the input, not describe it",
				)

	def test_a_missing_filename_names_filename(self):
		"""The required-field refusal a partner hits most often, driven through the real attach core
		against a real lead — `_create_one` resolves the lead BEFORE it checks the filename, so without
		one this test proves only that a missing lead is a 404."""
		lead = self._a_lead()

		def attach(**_kwargs):
			partner_file._create_one(frappe._dict({"lead": lead}), None, True)

		self.assertEqual(self._drive(attach)["error"].get("fields"), ["filename"])

	# -- 2.4: a batch that saved nothing is not a success --------------------

	def test_a_wholly_failed_batch_keeps_its_200_but_is_recorded_as_an_error(self):
		"""The partial-success envelope is the partner contract and must not move: 200, the same
		summary, the same per-record results. What was wrong is that NOTHING else knew the batch had
		failed — `request_error()` is what the request log reads, and it was empty."""
		def always_fails(_i, _item):
			frappe.throw("this record is not acceptable")

		_base._run_bulk([{"a": 1}, {"a": 2}], always_fails)
		resp = frappe.local.response

		self.assertEqual(resp["status"], "success", "the partial-success contract must not change")
		self.assertEqual(resp["summary"], {"total": 2, "succeeded": 0, "failed": 2})
		self.assertEqual(len(resp["results"]), 2)
		self.assertNotIn("error", resp, "the response body itself must stay clean")

		error = _base.request_error()
		self.assertTrue(error, "a batch where every record failed must not read back as a success")
		self.assertEqual(error["code"], "validation_error")
		self.assertIn("2 of 2", error["message"])
		self.assertEqual(error["detail"]["summary"]["failed"], 2)
		self.assertEqual([f["index"] for f in error["detail"]["failures"]], [0, 1])

	def test_a_fully_successful_batch_is_still_a_success(self):
		"""The other half of the same rule: nothing is stashed when nothing failed, so a clean batch
		cannot be mis-filed as an error by the fix for the one above."""
		_base._run_bulk([{"a": 1}], lambda i, item: {"index": i, "status": "success", "data": item})
		self.assertFalse(_base.request_error(), "a clean batch must record no error at all")

	def test_a_batch_is_filed_under_the_code_that_actually_dominated_it(self):
		"""The verdict took `failures[0]["code"]` — whichever record happened to fail first. One
		transient server_busy ahead of ninety-nine validation_errors filed the whole batch as an
		outage, and an operator triaging by error_code chased the wrong thing."""
		def fails_by_index(i, _item):
			if i == 0:
				raise frappe.ServiceUnavailableError("scanner down")
			frappe.throw("this record is not acceptable")

		_base._run_bulk([{"a": i} for i in range(5)], fails_by_index)
		self.assertEqual(_base.request_error()["code"], "validation_error")

	def test_a_huge_failed_batch_does_not_write_a_huge_log_row(self):
		"""`detail["failures"]` held EVERY failure and observability writes it, as JSON, to a Code
		column on the logging hot path — a 5000-record all-fail batch wrote a multi-megabyte row while
		the controller docstring promised the insert was "kept light". The sample is capped; `summary`
		still carries the true total, so nothing is lost, only unrepeated."""
		def always_fails(_i, _item):
			frappe.throw("this record is not acceptable")

		count = _base._cfg()["bulk_max_records"]  # the real per-call ceiling; the sample must bite below it
		_base._run_bulk([{"a": i} for i in range(count)], always_fails)
		detail = _base.request_error()["detail"]

		self.assertEqual(len(detail["failures"]), _base._BULK_FAILURE_SAMPLE)
		self.assertEqual(detail["failures_sampled"], _base._BULK_FAILURE_SAMPLE)
		self.assertEqual(detail["summary"]["failed"], count, "the true total must survive the cap")

	def test_a_batch_verdict_cannot_smuggle_an_undeclared_error_code(self):
		"""The batch verdict wrote its code straight to a request-local that `request_error()` returns
		into the log's error_code column — around `_fail`, and so around the closed-vocabulary gate
		`_fail` enforces. Both writers now pass through `checked_code`."""
		class Unmapped(Exception):
			pass

		def always_fails(_i, _item):
			raise Unmapped("nothing in _ERROR_MAP matches this")

		_base._run_bulk([{"a": 1}], always_fails)
		self.assertIn(_base.request_error()["code"], _base.ERROR_CODES)
