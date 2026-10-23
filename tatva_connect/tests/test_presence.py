# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every app gets its idle threshold from Tatva Platform Settings through presence.client; blank reads the default, out of range is refused."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect import presence
from tatva_connect.access.doctype.tatva_platform_settings.tatva_platform_settings import SETTINGS


class TestPresenceIdleSetting(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def _set(self, minutes):
		doc = frappe.get_doc(SETTINGS)
		doc.presence_idle_minutes = minutes
		doc.save(ignore_permissions=True)

	def test_the_apps_get_the_operators_idle_threshold(self):
		self._set(12)
		self.assertEqual(presence.client()["idle_seconds"], 720)
		self._set(0)
		self.assertEqual(presence.client()["idle_seconds"], presence.IDLE_SECONDS, "a blank setting reads the default")

	def test_an_idle_threshold_outside_its_range_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._set(500)
