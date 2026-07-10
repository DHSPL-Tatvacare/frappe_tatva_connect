# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Automation Queue report scopes lead visibility.

AUDIT REGRESSION: the report enriches each parked row with its subject lead's name and grain. If that
join uses `frappe.get_all` (which ignores permissions), a caller who can read the queue doctype but NOT
the underlying leads sees other grains' patient names. In a healthcare CRM that is a PHI leak. The fix
routes the join through `frappe.get_list` (permission-checked) and drops any row whose lead the caller
cannot see, so the row's very existence is scoped too.

Oracle: the real permission model. `Automation Manager` is granted read on CRM Automation Resume but has
NO CRM Lead read (verified in the app's DocPerms), so it is the exact "can see the queue, cannot see the
lead" principal. As that user the report must return zero rows for a lead it cannot read; as a full
reader (Administrator) the same row appears. The contrast is the gate.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import dispatcher, versions
from tatva_connect.tatva_connect.report.automation_queue.automation_queue import execute
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist

_RULE_DT = "CRM Automation Rule"
_GRAIN = GRAINS[0]
_AXES = (_GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])
_PREFIX = "QR-"
_USER = "queue-report-probe@example.com"


def _cleanup():
	like = ("like", f"{_PREFIX}%")
	rules = frappe.get_all(_RULE_DT, filters={"rule_name": like}, pluck="name")
	frappe.db.delete("CRM Automation Action", {"parent": like})
	frappe.db.delete("CRM Automation Resume", {"rule": ("in", rules or [""])})
	frappe.db.delete(versions.DOCTYPE, {"rule": ("in", rules or [""])})
	frappe.db.delete("CRM Automation Run Log", {"rule": ("in", rules or [""])})
	frappe.db.delete(_RULE_DT, {"rule_name": like})
	frappe.db.delete("CRM Lead", {"first_name": "QueueReport"})


class TestAutomationQueueReportScoping(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		frappe.set_user("Administrator")
		frappe.db.set_value("CRM Tatva Automation", "Task::Automation::rules", "enabled", 1)
		if not frappe.db.exists("User", _USER):
			user = frappe.get_doc({
				"doctype": "User", "email": _USER, "first_name": "Queue Report Probe",
				"send_welcome_email": 0, "roles": [{"role": "Automation Manager"}],
			}).insert(ignore_permissions=True)
		else:
			user = frappe.get_doc("User", _USER)
		cls.user = user.name

		lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "QueueReport", "lead_name": "Queue Report Patient", "status": "New",
			"custom_vertical": _AXES[0], "custom_group": _AXES[1], "custom_current_program": _AXES[2],
		}).insert(ignore_permissions=True)
		cls.lead = lead.name

		rule = frappe.get_doc({
			"doctype": _RULE_DT, "rule_name": f"{_PREFIX}drip", "enabled": 1,
			"on_doctype": "CRM Lead", "event": "Updated",
			"vertical": _AXES[0], "group": _AXES[1], "program": _AXES[2], "criteria": [],
			"actions": [
				{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "m1"},
				{"action_type": "Wait", "wait_expression": "{'days': 30}"},
				{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "m2"},
			],
		}).insert(ignore_permissions=True)
		dispatcher.run_effects(lead.name, versions.current_name(rule.name), lead, _AXES, "grain", {}, {})

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_cleanup()
		frappe.delete_doc("User", _USER, force=1, ignore_permissions=True)
		frappe.db.commit()  # user delete issues DDL-adjacent writes the harness rollback cannot undo

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_full_reader_sees_the_parked_lead(self):
		frappe.set_user("Administrator")
		_columns, data, _msg, _chart, _summary = execute({})
		self.assertIn(self.lead, [r["lead"] for r in data], "a full reader must see the parked execution")

	def test_queue_reader_without_lead_access_sees_no_patient_rows(self):
		"""The probe user can read the queue doctype but not the lead. The report must not leak the
		patient name or even the row's existence."""
		frappe.set_user(self.user)
		_columns, data, _msg, _chart, _summary = execute({})
		leaked = [r for r in data if r["lead"] == self.lead]
		self.assertEqual(leaked, [], "a caller who cannot read the lead must not see its queue row")
		names = [r.get("lead_name") for r in data]
		self.assertNotIn("Queue Report Patient", names, "the patient name leaked to a non-lead-reader")


if __name__ == "__main__":
	unittest.main()
