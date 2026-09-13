"""The membership encoding, locked in the language that EVALUATES it.

`is one of` / `is not one of` hold several values in one stored string, split on a comma OR a newline by
`rules._split_list`. The workflow canvas writes that string, and `frontend/src/tatva/predicateList.js` is
the same sentence in JavaScript. Two languages, one rule — so this file and
`frontend/tests/unit/predicateList.test.js` assert the SAME table, and the values below are the ones the
live Anaya flows actually hold, read from `CRM Workflow Node` on 2026-09-12.

What it protects: a control that reads a condition and writes it back must not change which records it
selects. Not "looks right" — the same items, in the same order.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import rules

# Verbatim from production. Workflow configuration, not patient data.
LIVE_CONDITIONS = {
	"source is one of": "Landing Page MR Form,Mobile App,Tucatinib,Ujvira,Nivolumab",
	"source is not one of": "Inbound Phone call,Goodflip",
	"source is not one of (6)": "Inbound Email,Inbound Phone call,Outbound Phone call,Goodflip,Tucatinib,Ujvira",
	"outcome is one of": "Connected - Call back later,Not Connected",
	"outcome is one of (2)": "Call back later,Not Connected",
	"connected_status is one of": (
		"Chemo Completed - Marked attendance on Portal and asked patient to upload discharge summary,"
		"Chemo Completed- Marked attendance on Portal and Documents Uploaded"
	),
	"custom_substage is not one of": (
		"Sigrima::Patient no more\nUjvira::Patient no more\nNivolumab::Patient no more\n"
		"Tukavo::Patient no more\nSigrima::Test/Junk lead"
	),
}

# What the canvas writes: the chosen values, joined. Mirrors `predicateList.joinItems`.
JOIN = "\n"


def _join(items):
	return JOIN.join(v.strip() for v in items if v and v.strip())


class TestMembershipRoundtrip(FrappeTestCase):
	def test_every_live_condition_selects_the_same_records_after_a_roundtrip(self):
		"""Read a stored condition into values, write it back, read it again — identical items.

		This is the sign-off: no live flow changes meaning because a picker touched it.
		"""
		for name, stored in LIVE_CONDITIONS.items():
			with self.subTest(condition=name):
				before = rules._split_list(stored)
				after = rules._split_list(_join(before))
				self.assertEqual(before, after, f"{name} changed meaning on a roundtrip")
				self.assertTrue(before, "a live condition must not read as empty")

	def test_both_separators_read_the_same(self):
		"""Everything stored today is comma-joined; everything written from now on is newline-joined."""
		self.assertEqual(rules._split_list("a,b"), ["a", "b"])
		self.assertEqual(rules._split_list("a\nb"), ["a", "b"])
		self.assertEqual(rules._split_list("a,b\nc"), ["a", "b", "c"])

	def test_blanks_and_padding_are_dropped_not_stored_as_a_value(self):
		self.assertEqual(rules._split_list(" a , b ,,\n "), ["a", "b"])
		self.assertEqual(rules._split_list(""), [])
		self.assertEqual(rules._split_list(None), [])

	def test_a_value_containing_a_comma_cannot_be_expressed(self):
		"""Stated, because it is the ENGINE's limit and no separator choice in the UI changes it.

		One live `CRM Task Type` carries commas in its own name. Whatever the control writes, this splitter
		fragments it — so the control names such a value to the author instead of saving a dead condition.
		The day `_split_list` learns to accept a real list, this test is the one that must be rewritten.
		"""
		comma = "Goodflip::India::Inside-Sales::Device, Lab Tests,  Tickets related and  other calls"
		self.assertNotEqual(rules._split_list(_join([comma, "Other"])), [comma, "Other"])
		self.assertEqual(len(rules._split_list(_join([comma, "Other"]))), 4)

	def test_a_real_list_is_still_not_understood(self):
		"""The reason the control writes a STRING today. Delete this when the server accepts a list."""
		self.assertEqual(rules._split_list(["Goodflip", "Ujvira"]), ["['Goodflip'", "'Ujvira']"])

	def test_a_single_value_stays_a_single_value(self):
		self.assertEqual(rules._split_list("Connected"), ["Connected"])
		self.assertEqual(_join(["Connected"]), "Connected")
