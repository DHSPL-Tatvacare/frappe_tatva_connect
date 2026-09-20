# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An unset clock is never due, and no scheduler asks that question its own way.

Frappe rewrites a range filter as `IFNULL(col, '0001-01-01') <= now`, so a column left NULL to mean "no clock"
reads as overdue since the year 1. A finished cohort clears its clock, was therefore due on every pass, and
re-ran its whole population - every patient a second journey, every pass, for as long as it stayed Active."""

import ast
import inspect
import pathlib

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import utils

# {module: the clock each scheduler asks about} — every one of them reads NULL as "no clock", never as "overdue".
_CLOCKS = {
	"tatva_connect/workflow_engine/cohort.py": "trigger_next_run_at",
	"tatva_connect/workflow_engine/wakeups.py": "resume_at",
	"tatva_connect/whatsapp/media_retry.py": "custom_media_next_attempt_at",
	"tatva_connect/storage/call_media.py": "recording_next_attempt_at",
	"tatva_connect/api/partner_bulk_job.py": "finished_at",
	"tatva_connect/api/partner_bulk_worker.py": "started_at",
}
_RANGE = {"<", "<=", ">", ">="}
_DT = "CRM Bulk Job"


def _app_root():
	return pathlib.Path(inspect.getfile(utils)).parent


class TestAnUnsetClockIsNeverDue(FrappeTestCase):
	def _job(self, finished_at=None):
		"""A bulk job owned by the caller, because `finished_at` is one of the six clocks this rule governs - not a stand-in for one."""
		doc = frappe.get_doc({"doctype": _DT, "partner": frappe.session.user, "operation": "lead_create",
		                      "input_format": "inline", "status": "JobComplete",
		                      "finished_at": finished_at}).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, _DT, doc.name, force=True)
		return doc.name

	def test_due_now_excludes_a_row_whose_clock_is_unset(self):
		"""The behaviour itself, over a real table: the row with no clock must not come back."""
		passed = self._job(frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-1))
		unset = self._job()

		due = utils.due_now(_DT, "finished_at", filters=[["partner", "=", frappe.session.user]], pluck="name")

		self.assertIn(passed, due, "a row whose clock has passed was not due")
		self.assertNotIn(unset, due, "a row with NO clock came back as due")

	def test_a_bare_range_filter_on_that_clock_would_have_matched_it(self):
		"""The evasion, proven rather than asserted: the query this replaced returns the unset row."""
		unset = self._job()

		bare = frappe.get_all(_DT, filters={"partner": frappe.session.user,
		                                    "finished_at": ["<=", frappe.utils.now_datetime()]}, pluck="name")

		self.assertIn(unset, bare, "IFNULL no longer makes an unset clock overdue - re-read this test")

	def test_every_scheduler_asks_through_due_now(self):
		"""Auto-discovered: a module that range-filters its own clock has written a second, wrong answer."""
		for module, clock in _CLOCKS.items():
			path = _app_root() / pathlib.Path(module).relative_to("tatva_connect")
			with self.subTest(module=module):
				self.assertFalse(
					_range_filters_on(path.read_text(), clock),
					f"{module} compares {clock} with a range operator directly; ask utils.due_now instead",
				)


def _range_filters_on(source, clock):
	"""True when the source builds a `[clock, <range op>, ...]` or `{clock: [<range op>, ...]}` filter itself."""
	found = []
	for node in ast.walk(ast.parse(source)):
		if isinstance(node, ast.Dict):
			for key, value in zip(node.keys, node.values, strict=False):
				if isinstance(key, ast.Constant) and key.value == clock:
					found.append(_is_range(value))
		if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant):
			if node.elts[0].value == clock and len(node.elts) > 1:
				found.append(isinstance(node.elts[1], ast.Constant) and node.elts[1].value in _RANGE)
	return any(found)


def _is_range(value):
	return (isinstance(value, ast.List) and value.elts and isinstance(value.elts[0], ast.Constant)
	        and value.elts[0].value in _RANGE)
