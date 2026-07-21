# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CRM API Request Log must be readable, complete, and must not outlive its usefulness.

The request log is the ONE place an operator looks when a partner reports a failure, and it was the
one place the failure was invisible:

  * a bulk call where 100 of 100 records were refused recorded HTTP 200, is_error 0, blank error
    columns — indistinguishable from a clean run;
  * the screening verdict that caused the refusal lived only in CRM File Scan Log, reachable only by
    a manual trace_id join;
  * the list view showed no reason at all without opening a row.

Retention is NOT a hook here, and the test below pins why. Both logs register themselves with Log
Settings from their ACTIVATORS (`capture.apply_logging`, `file_screening.apply_scan_logging`), tied to
the operator toggle that turns each feature on. A `default_log_clearing_doctypes` hook would be a
second decider and it would win: `LogSettings.validate()` calls `add_default_logtypes()`, which
recomputes from the hook against the post-removal child table — so deregistering becomes a no-op
inside the very save that performs it, and retention runs while the feature is dormant.

Rows written here are COMMITTED (log_request commits its own row, by design — see its docstring), so
a savepoint cannot undo them and every test deletes what it wrote.
"""
import unittest
from unittest.mock import patch

import frappe
from frappe.desk.form.meta import get_meta as get_form_meta

from tatva_connect.api import _base
from tatva_connect.observability import capture
from tatva_connect.storage import file_screening
from tatva_connect.tests.api.test_error_truthfulness import screen_under_stubbed_config

_LOG = "CRM API Request Log"
_SCAN_LOG = "CRM File Scan Log"
_PATH = _base._PARTNER_PATH + "_file.file_attach_bulk"


class TestRequestLogTruthfulness(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		self._form = frappe.form_dict
		self._response = frappe.local.response
		frappe.form_dict = frappe._dict()
		frappe.local.response = frappe._dict()
		frappe.local.partner_bulk_error = None
		self._written = []

	def tearDown(self):
		frappe.form_dict = self._form
		frappe.local.response = self._response
		frappe.local.partner_bulk_error = None
		for name in self._written:
			frappe.db.delete(_LOG, {"name": name})
		frappe.db.commit()

	def _capture(self, status_code=200):
		"""Run the REAL after_request logger and return the row it actually wrote."""
		before = frappe.db.sql(f"select max(name) from `tab{_LOG}`")[0][0] or 0
		with patch.object(capture.automation, "is_enabled", return_value=True):
			capture.log_request(
				response=frappe._dict(status_code=status_code),
				request=frappe._dict(path=_PATH, method="POST"),
			)
		name = frappe.db.sql(f"select max(name) from `tab{_LOG}`")[0][0] or 0
		self.assertGreater(name, before, "the watched endpoint must have produced a log row")
		self._written.append(name)
		return frappe.get_doc(_LOG, name)

	# -- 2.4 seen from the log side ------------------------------------------

	def test_a_batch_that_saved_nothing_is_not_filed_as_a_clean_success(self):
		"""A 200 is the partner contract for a partial success, so the status code cannot be the error
		test. The log read only `frappe.local.response["error"]`, which a bulk call never sets — so the
		worst possible outcome (nothing saved) was recorded identically to the best one."""
		def always_fails(_i, _item):
			frappe.throw("this record is not acceptable")

		_base._run_bulk([{"a": 1}, {"a": 2}], always_fails)
		row = self._capture(status_code=200)

		self.assertEqual(row.status_code, 200, "the response really was a 200 — that is the contract")
		self.assertEqual(row.is_error, 1, "a batch where every record failed is not a success")
		self.assertEqual(row.error_code, "validation_error")
		self.assertIn("2 of 2", row.error_message)
		self.assertEqual(frappe.parse_json(row.error_detail)["summary"]["failed"], 2)

	def test_a_clean_call_is_still_logged_clean(self):
		"""The inverse, so the fix above cannot mark every row an error."""
		row = self._capture(status_code=200)
		self.assertEqual(row.is_error, 0)
		self.assertIsNone(row.error_code)
		self.assertIsNone(row.error_detail)

	# -- 2.2 seen from the log side ------------------------------------------

	def test_the_screening_verdict_is_readable_without_a_manual_join(self):
		"""The verdict reached CRM File Scan Log richly and the request log not at all. `error.detail`
		is what the API already decided; the log persists it verbatim (core's own choice of the Code
		fieldtype for a structured blob, as Error Log.metadata does) and never re-derives it.

		Driven through the REAL producer — `screen()` -> `_block_exception` -> `_classify` -> `_fail`.
		Hand-feeding `_fail` a literal detail dict proved only that a dict survives `as_json`; it would
		have stayed green through any change to the shape screening actually emits."""
		def attach(**_kwargs):
			screen_under_stubbed_config(verdict_scan=("infected", "Eicar-Test-Signature"))

		_base._api(attach)()
		self.assertEqual(frappe.local.response["http_status_code"], 400,
		                 "the real producer must have refused before the log is asked about it")
		row = self._capture(status_code=400)

		self.assertEqual(row.is_error, 1)
		detail = frappe.parse_json(row.error_detail)
		self.assertEqual(detail["check"], "file_screening")
		self.assertEqual(detail["verdict"], "Infected")
		self.assertEqual(detail["signature"], "Eicar-Test-Signature")
		self.assertEqual(detail["file_name"], "report.pdf")

	# -- 2.5 readable at a glance --------------------------------------------

	def test_the_list_shows_why_a_call_failed_without_opening_it(self):
		"""error_code and error_message existed on the row and appeared in no list column, so triaging a
		partner incident meant opening records one by one."""
		meta = frappe.get_meta(_LOG)
		for fieldname in ("error_code", "error_message"):
			with self.subTest(field=fieldname):
				self.assertEqual(meta.get_field(fieldname).in_list_view, 1)

		for fieldname in ("channel", "endpoint", "source", "is_error", "error_code"):
			with self.subTest(filter=fieldname):
				self.assertEqual(
					meta.get_field(fieldname).in_standard_filter, 1,
					f"{fieldname} is what an operator filters by; it must be a standard filter",
				)

		detail = meta.get_field("error_detail")
		self.assertIsNotNone(detail, "the structured verdict needs a home on the row")
		self.assertEqual(detail.fieldtype, "Code", "core's own choice for a structured blob on a log row")
		self.assertEqual(detail.read_only, 1)

	def test_the_list_view_colours_errors_red(self):
		"""No listview settings existed, so every row rendered the same neutral badge. The indicator
		returns frappe's [label, colour, filter] triple, which is what makes the badge click into a
		filter — mirrors core's error_log_list.js.

		Read through frappe's OWN asset loader (`FormMeta.load_assets` -> `__list_js`), not off disk: a
		file that exists proves nothing, because a misnamed or misplaced one is never loaded and the
		list renders exactly as it did before. `__list_js` non-empty is the framework saying it took it."""
		list_js = get_form_meta(_LOG, cached=False).get("__list_js") or ""
		self.assertTrue(list_js, "frappe must actually LOAD the list js, not merely find a file on disk")

		self.assertIn('frappe.listview_settings["CRM API Request Log"]', list_js)
		self.assertIn("get_indicator", list_js)
		self.assertIn('"red", "is_error,=,1"', list_js)
		self.assertIn('"green", "is_error,=,0"', list_js)

	# -- retention belongs to the toggle, not to a hook -----------------------

	def _registered_days(self, doctype):
		"""The retention Log Settings currently holds for `doctype`, or None if it holds none."""
		row = next((r for r in frappe.get_doc("Log Settings").logs_to_clear
		            if r.ref_doctype == doctype), None)
		return int(row.days) if row else None

	def test_the_operator_toggle_really_owns_log_settings_registration(self):
		"""Retention must follow the feature, both ways.

		The first attempt at this asserted a `default_log_clearing_doctypes` hook existed. That hook is
		a SECOND decider and it defeats the first: `LogSettings.validate()` runs `add_default_logtypes()`
		against the post-removal child table, so the activator's `settings.remove(row); settings.save()`
		re-appends the row in the same save. Deregistration silently became a no-op and retention ran on
		a dormant feature — the exact shape of a default-on automation. The hook is gone; this pins that
		the ACTIVATORS, and only they, decide."""
		for doctype, activator in ((_LOG, capture.apply_logging),
		                           (_SCAN_LOG, file_screening.apply_scan_logging)):
			with self.subTest(doctype=doctype):
				was = self._registered_days(doctype)
				try:
					activator(True)
					self.assertEqual(
						self._registered_days(doctype), 90,
						"turning the feature on must register the log at the retention its controller uses",
					)
					activator(False)
					self.assertIsNone(
						self._registered_days(doctype),
						"turning the feature off must really DEREGISTER it — a row that survives here is "
						"retention running on a dormant feature",
					)
				finally:
					# Operator config is not ours to leave changed: put the site back as we found it.
					activator(was is not None)
					frappe.db.commit()
