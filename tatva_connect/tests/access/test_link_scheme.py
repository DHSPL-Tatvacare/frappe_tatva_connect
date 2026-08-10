# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Link scheme safety — https:// only for every value that reaches a browser as a link/redirect.
Driven through real .save() so a refactoring to a dormant switch or a different hook returns red.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access.link_scheme import is_safe_scheme


class TestLinkScheme(FrappeTestCase):
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


class TestWebsiteFieldScheme(FrappeTestCase):
	"""website field on CRM Lead / CRM Deal / CRM Organization — https:// only."""

	def test_http_website_rejected_on_crm_lead(self):
		lead = frappe.get_last_doc("CRM Lead")
		lead.website = "http://example.test"
		with self.assertRaises(frappe.ValidationError):
			lead.save()

	def test_https_website_accepted_on_crm_lead(self):
		lead = frappe.get_last_doc("CRM Lead")
		lead.website = "https://tatvacare.in"
		lead.save(ignore_permissions=True)

	def test_empty_website_accepted_on_crm_lead(self):
		lead = frappe.get_last_doc("CRM Lead")
		lead.website = ""
		lead.save(ignore_permissions=True)

	def test_unchanged_website_not_rejudged(self):
		lead = frappe.get_last_doc("CRM Lead")
		lead.website = "https://tatvacare.in"
		lead.save(ignore_permissions=True)
		lead.reload()
		lead.set("first_name", lead.first_name)
		lead.website = None
		lead.save(ignore_permissions=True)


class TestMapsSettingsScheme(FrappeTestCase):
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
