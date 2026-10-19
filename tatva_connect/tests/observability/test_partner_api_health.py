# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Control Tower and Integration Endpoint Breakdown read the request log the way the log itself reads.

A bulk call that refused every record is a 200 with `is_error` 1, so a failure count keyed on the status code
missed it; the rollup warning read the configured delay instead of how far behind the rollup is; and the
endpoint report failed whenever Integration or Lane was left blank, which is how "all" is asked for.

Rows are committed so the report's own query sees them, and every test deletes what it wrote.
"""
import unittest
from unittest.mock import patch

import frappe
from frappe.desk.query_report import run as run_report
from frappe.utils import add_to_date, now_datetime

from tatva_connect.observability import control_tower

_LOG = "CRM API Request Log"
_SOURCE = "test-partner-api-health@example.com"


class TestPartnerApiHealth(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.delete(_LOG, {"source": _SOURCE})
		frappe.db.commit()

	def _log(self, status_code, is_error, endpoint="lead_create_bulk", channel="Partner API"):
		frappe.get_doc({
			"doctype": _LOG, "request_time": now_datetime(), "channel": channel, "endpoint": endpoint,
			"source": _SOURCE, "http_method": "POST", "status_code": status_code, "is_error": is_error,
		}).insert(ignore_permissions=True)
		frappe.db.commit()

	def test_a_failed_200_counts_and_the_lag_is_the_real_gap(self):
		before = control_tower.ControlTower().partner_api()["failed_1h"]
		self._log(status_code=200, is_error=1)
		self.assertEqual(control_tower.ControlTower().partner_api()["failed_1h"], before + 1)

		thirteen_hours_ago = add_to_date(now_datetime(), hours=-13)
		with patch.object(frappe.db, "get_singles_dict", return_value={"lag_seconds": 300, "last_rolled_until": thirteen_hours_ago}):
			api = control_tower.ControlTower().partner_api()
		self.assertGreaterEqual(api["rollup_lag_seconds"], 13 * 3600 - 60)
		problems = control_tower._problems({"meta": {"sections_failed": []}, "tatva": {"partner_api": api}})
		self.assertTrue(any("rollup is 13h behind" in p for p in problems), problems)

	def test_the_endpoint_breakdown_runs_with_integration_and_lane_left_blank(self):
		self._log(status_code=200, is_error=0, endpoint="lead_get")
		since = add_to_date(now_datetime(), hours=-1)

		everything = _rows(run_report("Integration Endpoint Breakdown", filters={"from_datetime": since}))
		self.assertIn("lead_get", [row["endpoint"] for row in everything])

		one = _rows(run_report("Integration Endpoint Breakdown", filters={"from_datetime": since, "integration": _SOURCE, "lane": "Partner API"}))
		self.assertEqual([(r["endpoint"], r["calls"]) for r in one], [("lead_get", 1)])


def _rows(report):
	"""The data rows; the report's own total row comes back as a list, not a dict."""
	return [row for row in report["result"] if isinstance(row, dict)]
