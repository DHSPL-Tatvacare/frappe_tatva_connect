# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The hover preview reads ONE declaration, gates before it reads, and reads once.

Three things can go wrong with a preview payload and all three have gone wrong elsewhere in this app:

  a second brain   the client names the fields it wants, so the card and the side panel drift the first
                   time an operator edits the layout. Asserted below by reading the expectation OFF the
                   layout — the test never lists a fieldname of its own.
  a probe          a refusal that says "no such lead" for one id and "not permitted" for another turns a
                   mouse-over into a way to enumerate record ids. Both must raise PermissionError.
  an N+1           one card open must be one document read, whatever the layout declares. A per-field
                   lookup would be invisible on a dev site and a stampede on a real list.

`TestTheFieldWalk` needs no site: the walk is pure given a declaration and a document, so it runs under
plain `python -m unittest` as well as under the bench, and it is what pins the skip rules.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.api.test_lead_preview
"""
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import lead_preview

USER = "zz-lead-preview@example.com"
PHONE = "+916100060001"
MISSING = "CRM-LEAD-0000-99999"


class TestLeadPreview(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		if not frappe.db.exists("User", USER):
			# Default roles only: no CRM DocPerm at all, so the read gate refuses at the doctype level.
			frappe.get_doc({
				"doctype": "User", "email": USER, "first_name": "Lead Preview",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
		cls.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Preview", "last_name": "Patient",
			"mobile_no": PHONE, "status": "New",
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		if frappe.db.exists("User", USER):
			frappe.delete_doc("User", USER, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": PHONE}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def test_the_card_is_a_CLOSED_set_of_keys(self):
		"""The card is six things and the list is closed. A key appearing here that nobody designed is the
		defect this test exists for: the payload must never grow with an operator's layout edit again."""
		card = lead_preview.get_lead_preview(self.lead)
		self.assertEqual(
			set(card),
			{"name", "title", "image", "phone", "stage", "stage_color", "owner", "source", "grain"},
		)

	def test_it_answers_with_the_documents_own_values(self):
		"""The card renders what it is handed and derives nothing (E2)."""
		doc = frappe.get_doc("CRM Lead", self.lead)
		card = lead_preview.get_lead_preview(self.lead)
		self.assertEqual(card["name"], doc.name)
		self.assertEqual(card["title"], doc.lead_name or doc.first_name)
		self.assertEqual(card["phone"], doc.mobile_no)
		self.assertEqual(card["owner"], doc.lead_owner or "")

	def test_the_owner_is_an_EMAIL_not_a_resolved_name(self):
		"""Resolving a user to a name here would be a second answer to "who is this person" — the browser's
		users store, which the Assigned To column already reads, is the one that answers it."""
		card = lead_preview.get_lead_preview(self.lead)
		self.assertNotIn(" ", card["owner"], "the owner was resolved server-side")

	def test_the_grain_carries_only_the_axes_that_are_set(self):
		"""A blank axis is not a line on the card, and each axis keeps its own label."""
		card = lead_preview.get_lead_preview(self.lead)
		for axis in card["grain"]:
			self.assertEqual(set(axis), {"label", "value"})
			self.assertTrue(axis["value"], "a blank grain axis reached the card")

	def test_the_stage_carries_its_own_colour_off_the_master(self):
		"""The colour is the stage master's data, never a client map — so the hover card and the spotlight
		search can never disagree about what colour a stage is. Unset is normal and renders neutral."""
		doc = frappe.get_doc("CRM Lead", self.lead)
		key = doc.custom_substage or doc.custom_stage
		card = lead_preview.get_lead_preview(self.lead)
		if not key:
			self.assertEqual((card["stage"], card["stage_color"]), ("", ""))
			return
		row = frappe.db.get_value("CRM Lead Stage", key, ["display_label", "stage", "color"], as_dict=True)
		self.assertEqual(card["stage"], row.display_label or row.stage or "")
		self.assertEqual(card["stage_color"], row.color or "")

	def test_one_card_open_is_one_document_read(self):
		"""The lead is loaded once — no per-field lookup, no N+1."""
		real = frappe.get_cached_doc
		with patch.object(frappe, "get_cached_doc", side_effect=real) as loader:
			lead_preview.get_lead_preview(self.lead)
		reads = [c for c in loader.call_args_list if c.args and c.args[0] == "CRM Lead"]
		self.assertEqual(len(reads), 1, f"the preview read CRM Lead {len(reads)} times for one card")

	def test_a_lead_the_caller_may_not_read_is_refused(self):
		frappe.set_user(USER)
		try:
			with self.assertRaises(frappe.PermissionError):
				lead_preview.get_lead_preview(self.lead)
		finally:
			frappe.set_user("Administrator")

	def test_a_lead_that_does_not_exist_is_refused_the_SAME_way(self):
		"""Missing and unreadable answer identically, or a hover becomes an id-enumeration oracle."""
		for user in (USER, "Administrator"):
			frappe.set_user(user)
			try:
				with self.assertRaises(frappe.PermissionError):
					lead_preview.get_lead_preview(MISSING)
			finally:
				frappe.set_user("Administrator")
