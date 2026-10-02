# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Link scheme safety — https:// only for every value that reaches a browser as a link/redirect.
Driven through real .save() so a refactoring to a dormant switch or a different hook returns red.
"""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.access.link_scheme import is_safe_scheme


class TestLinkScheme(IntegrationTestCase):
	"""Pure-function tests: the normaliser and predicate."""

	def test_https_accepted(self):
		self.assertTrue(is_safe_scheme("https://tatvacare.in"))
		self.assertTrue(is_safe_scheme("https://example.test/path?q=1"))

	def test_empty_accepted(self):
		self.assertTrue(is_safe_scheme(""))
		self.assertTrue(is_safe_scheme(None))

	def test_http_rejected(self):
		self.assertFalse(is_safe_scheme("http://example.test"))
		self.assertFalse(is_safe_scheme("HTTP://EXAMPLE.TEST"))

	def test_javascript_rejected(self):
		self.assertFalse(is_safe_scheme("javascript:alert(1)"))

	def test_data_rejected(self):
		self.assertFalse(is_safe_scheme("data:text/html,<script>alert(1)</script>"))

	def test_tab_before_scheme_cannot_hide_javascript(self):
		self.assertTrue(is_safe_scheme("https://\texample.test"))
		self.assertFalse(is_safe_scheme("java\tscript:alert(1)"))


class TestMapsSettingsScheme(IntegrationTestCase):
	"""osm_tile_url on CRM Maps Settings (a Single) — rendered as the leaflet tile src, https:// only.
	Driven through real .save() so the guard is proven at the write, not just as a pure function."""

	def test_http_tile_url_rejected(self):
		s = frappe.get_single("CRM Maps Settings")
		s.osm_tile_url = "http://tiles.example.test/{z}/{x}/{y}.png"
		with self.assertRaises(frappe.ValidationError) as cm:
			s.save(ignore_permissions=True)
		self.assertIn("secure web address", str(cm.exception))

	def test_javascript_tile_url_rejected(self):
		s = frappe.get_single("CRM Maps Settings")
		s.osm_tile_url = "javascript:alert(1)"
		with self.assertRaises(frappe.ValidationError) as cm:
			s.save(ignore_permissions=True)
		self.assertIn("secure web address", str(cm.exception))

	def test_https_tile_url_accepted(self):
		s = frappe.get_single("CRM Maps Settings")
		s.osm_tile_url = "https://tiles.example.test/{z}/{x}/{y}.png"
		s.save(ignore_permissions=True)


class TestALeadWebsiteMustBeHttps(IntegrationTestCase):
	"""Through a real CRM Lead save, so unhooking the guard from the lead's website field goes red."""

	def _lead(self, website):
		return frappe.get_doc({"doctype": "CRM Lead", "first_name": "ZZ Scheme", "website": website})

	def test_an_http_or_script_website_is_refused(self):
		for url in ("http://example.test", "javascript:alert(1)"):
			with self.subTest(url=url), self.assertRaises(frappe.ValidationError):
				self._lead(url).insert(ignore_permissions=True)

	def test_an_https_website_saves(self):
		self.assertTrue(self._lead("https://example.test").insert(ignore_permissions=True).name)
