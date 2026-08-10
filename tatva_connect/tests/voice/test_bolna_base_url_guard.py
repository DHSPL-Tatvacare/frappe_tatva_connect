# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Bolna adapter's outbound base URL is SSRF-vetted before every call. Mirrors the guard proof in
tests/channels/test_transfer.py: patch the one shared guard to prove it is CALLED, plus one real
private-IP call (an IP literal needs no DNS) to prove the wiring refuses an internal host end to end.
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.voice.adapters import bolna


class TestBolnaBaseUrlGuard(FrappeTestCase):
	def test_operator_base_url_is_vetted(self):
		with patch("tatva_connect.voice.adapters.bolna.assert_safe_public_url") as guard:
			out = bolna._safe_base_url({"base_url": "https://provider.example/"})
		guard.assert_called_once_with("https://provider.example")
		self.assertEqual(out, "https://provider.example")

	def test_blank_falls_back_to_the_public_default_and_is_vetted(self):
		with patch("tatva_connect.voice.adapters.bolna.assert_safe_public_url") as guard:
			out = bolna._safe_base_url({})
		guard.assert_called_once_with("https://api.bolna.ai")
		self.assertEqual(out, "https://api.bolna.ai")

	def test_internal_host_is_refused(self):
		# An IP literal resolves with no DNS, so this is deterministic and offline.
		with self.assertRaises(frappe.ValidationError):
			bolna._safe_base_url({"base_url": "http://10.0.0.5"})
