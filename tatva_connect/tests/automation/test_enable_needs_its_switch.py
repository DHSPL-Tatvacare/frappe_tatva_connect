# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A record's own Enabled tick cannot be turned on while its feature switch is off: the save is refused in the one sentence
and title every surface uses, and a re-save of a record already on is never re-litigated. Proved through a real save."""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

DATASET_ID = "zz-enable-needs-switch"
_SWITCH_STATE = "tatva_connect.automation.settings.is_enabled"


class TestEnableNeedsItsSwitch(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def _dataset(self, enabled):
		return frappe.get_doc({
			"doctype": "CRM Meta Dataset", "dataset_name": "ZZ Enable Probe", "dataset_id": DATASET_ID,
			"enabled": enabled, "send_frequency": "Hourly", "access_token": "zz-token",
			"graph_api_version": "v23.0", "lead_event_source": "ZZ CRM",
		})

	def test_turning_it_on_with_the_switch_off_is_refused(self):
		with patch(_SWITCH_STATE, return_value=False), self.assertRaises(frappe.ValidationError) as refused:
			self._dataset(1).insert(ignore_permissions=True)
		self.assertIn("is turned off", str(refused.exception))

	def test_with_the_switch_on_it_saves(self):
		with patch(_SWITCH_STATE, return_value=True):
			self.assertEqual(self._dataset(1).insert(ignore_permissions=True).enabled, 1)

	def test_a_record_already_on_still_saves_with_the_switch_off(self):
		with patch(_SWITCH_STATE, return_value=True):
			doc = self._dataset(1).insert(ignore_permissions=True)
		with patch(_SWITCH_STATE, return_value=False):
			doc.dataset_name = "ZZ Enable Probe renamed"
			doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("CRM Meta Dataset", DATASET_ID, "dataset_name"), "ZZ Enable Probe renamed")
