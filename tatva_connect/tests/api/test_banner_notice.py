# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Blank is off, a script never ships, the id is the text, and a failed read is no notice — not a 500."""

import unittest
from unittest.mock import patch

from tatva_connect.api import banner


def _with_setting(value):
	return patch("frappe.db.get_single_value", return_value=value)


class TestTheNotice(unittest.TestCase):
	def test_blank_setting_is_no_notice(self):
		for blank in (None, "", "   "):
			with self.subTest(blank=blank), _with_setting(blank):
				self.assertEqual(banner.notice(), {})

	def test_a_notice_carries_its_html_and_an_id(self):
		with _with_setting("<div>Upgrade tonight</div>"):
			out = banner.notice()

		self.assertIn("Upgrade tonight", out["html"])
		self.assertTrue(out["id"])

	def test_script_is_stripped_before_it_is_published(self):
		hostile = '<div>Upgrade<script>alert(1)</script><img src=x onerror="alert(1)"></div>'
		with _with_setting(hostile):
			out = banner.notice()

		self.assertNotIn("<script", out["html"].lower())
		self.assertNotIn("onerror", out["html"].lower())
		self.assertIn("Upgrade", out["html"])

	def test_the_same_notice_keeps_its_id_and_an_edit_changes_it(self):
		with _with_setting("<div>Upgrade tonight</div>"):
			first = banner.notice()
		with _with_setting("<div>Upgrade tonight</div>"):
			again = banner.notice()
		with _with_setting("<div>Upgrade tomorrow</div>"):
			edited = banner.notice()

		self.assertEqual(first["id"], again["id"])
		self.assertNotEqual(first["id"], edited["id"])

	def test_a_failed_read_is_no_notice_not_an_exception(self):
		with patch("frappe.db.get_single_value", side_effect=Exception("no such table")):
			self.assertEqual(banner.notice(), {})

	def test_a_stale_shared_cache_does_not_hide_a_saved_notice(self):
		with (
			patch("frappe.get_single_value", return_value=None),
			patch("frappe.client_cache.get_doc", side_effect=AssertionError("shared cache read")),
			_with_setting("<div>Upgrade tonight</div>"),
		):
			out = banner.notice()

		self.assertIn("Upgrade tonight", out["html"])


class TestTheBootBag(unittest.TestCase):
	def test_the_key_is_on_the_bag_the_page_already_serves(self):
		"""The app reads `window.tatva_banner`; nothing else publishes it."""
		from tatva_connect.api import boot

		with (
			patch("tatva_connect.list_engine.derived.declaration_version", return_value="v1"),
			_with_setting("<div>Upgrade tonight</div>"),
		):
			keys = boot.keys()

		self.assertIn("Upgrade tonight", keys["tatva_banner"]["html"])
