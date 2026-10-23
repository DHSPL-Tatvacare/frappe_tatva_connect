# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The address lookup asks Google once per ~11 m spot and remembers it; a failed lookup is never remembered."""
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from tatva_connect.location import api


class TestReverseGeocodeCache(FrappeTestCase):
	def setUp(self):
		self.settings = frappe.get_doc("CRM Maps Settings")
		self._key_was = self.settings.get_password("google_maps_api_key", raise_exception=False)
		self.settings.google_maps_api_key = "test-server-key"
		self.settings.save(ignore_permissions=True)

	def tearDown(self):
		self.settings.reload()
		self.settings.google_maps_api_key = self._key_was or ""
		self.settings.save(ignore_permissions=True)

	def test_one_spot_asks_google_once_and_a_failure_is_never_kept(self):
		"""RED before: every refresh and every rep at the same clinic paid Google again for the same address."""
		lat, lng = 3.21456, 3.65432
		frappe.cache.delete_value(api._GEOCODE_KEY.format(lat, lng))
		reply = MagicMock(json=lambda: {"results": [{"formatted_address": "Test Clinic", "address_components": []}]})
		with patch.object(api.requests, "get", return_value=reply) as http:
			first = api._reverse_geocode(lat, lng)
			again = api._reverse_geocode(lat + 0.00001, lng)  # a refresh's drift inside the same ~11 m square
		self.assertEqual((http.call_count, again), (1, first))

		lost = 3.31456
		frappe.cache.delete_value(api._GEOCODE_KEY.format(lost, lng))
		with patch.object(api.requests, "get", side_effect=requests.Timeout()) as http:
			api._reverse_geocode(lost, lng)
			api._reverse_geocode(lost, lng)
		self.assertEqual(http.call_count, 2)  # an outage is retried, never served from memory
