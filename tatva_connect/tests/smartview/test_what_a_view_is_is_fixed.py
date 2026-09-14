# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What a view IS — its resource, its activity type, its grain — is decided when it is created.

THREE DEFECTS, ONE FUNCTION.

  * `upsert_view` replaced every field from the payload, so an update that simply OMITTED an axis blanked
    it — and a view declaring no axis is site-wide, offered to everyone entitled to nothing in particular
    (`smartview/permissions.py`). A partial save could widen a view's audience with no gate and no error.
  * `is_standard` had two rules: this endpoint refused it unless you were an operator, while `set_public`
    let any owner publish. The same act, allowed at one door and refused at the other.
  * An Activity view could be authored on any task type in the site. A Lead view's grain is clamped to
    the caller's entitlement on the way in; the type was not, so the one resource had two rules.

The clamp is at AUTHORING and deliberately not at read, exactly as a Lead view's grain is: a view shared
across business lines is a feature, and re-clamping the reader would make handing one on silently do
nothing.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_what_a_view_is_is_fixed
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as smartview
from tatva_connect.tests.api import partner_fixture

LABEL = "ZZ Fixed Identity View"


class TestWhatAViewIsIsFixed(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		partner_fixture.mint_grain()
		cls.view = smartview.upsert_view({
			"label": LABEL, "base_object": "Lead",
			"vertical": partner_fixture.VERTICAL, "group": partner_fixture.GROUP,
		})["name"]
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Smart View", filters={"label": ["like", "ZZ Fixed Identity%"]}, pluck="name"):
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)

	def _row(self):
		return frappe.db.get_value(
			"CRM Smart View", self.view,
			["vertical", "group", "program", "base_object", "is_standard", "owner_user"], as_dict=True,
		)

	def test_an_update_that_omits_the_grain_does_not_blank_it(self):
		"""THE defect: a payload without axes made a scoped view site-wide."""
		smartview.upsert_view({"name": self.view, "label": LABEL, "base_object": "Lead"})
		row = self._row()
		self.assertEqual(row.vertical, partner_fixture.VERTICAL, "the grain must survive a partial save")
		self.assertEqual(row.group, partner_fixture.GROUP)

	def test_an_update_that_omits_the_presentation_fields_does_not_blank_them(self):
		"""The same defect on the fields a rep can SEE: description, colour and icon were written from the
		payload unconditionally, so a save that did not resend them wiped the tab's colour and icon."""
		smartview.upsert_view({"name": self.view, "label": LABEL, "base_object": "Lead",
		                       "description": "ZZ described", "color": "blue", "icon": "star"})
		smartview.upsert_view({"name": self.view, "label": LABEL, "base_object": "Lead"})
		row = frappe.db.get_value("CRM Smart View", self.view, ["description", "color", "icon"], as_dict=True)
		self.assertEqual(row.color, "blue", "a partial save must not undo what it never carried")
		self.assertEqual(row.icon, "star")
		self.assertEqual(row.description, "ZZ described")

	def test_the_presentation_fields_can_still_be_cleared(self):
		"""Omitted is untouched; SENT EMPTY is cleared — or the author could never take a colour off."""
		smartview.upsert_view({"name": self.view, "label": LABEL, "base_object": "Lead",
		                       "description": "", "color": "", "icon": ""})
		row = frappe.db.get_value("CRM Smart View", self.view, ["description", "color", "icon"], as_dict=True)
		self.assertFalse(row.color or row.icon or row.description)

	def test_an_update_cannot_move_a_view_to_another_grain(self):
		"""What a view is, is fixed: the editor disables the control, and now so does the server."""
		smartview.upsert_view({
			"name": self.view, "label": LABEL, "base_object": "Lead",
			"vertical": "ZZ Not This One", "group": "ZZ Nor This",
		})
		self.assertEqual(self._row().vertical, partner_fixture.VERTICAL)

	def test_an_update_cannot_change_the_resource(self):
		smartview.upsert_view({"name": self.view, "label": LABEL, "base_object": "Activity"})
		self.assertEqual(self._row().base_object, "Lead")

	def test_publishing_is_not_this_endpoints_job(self):
		"""One field, one door — `set_public` owns `is_standard`, the way `set_column_widths` owns widths."""
		smartview.upsert_view({"name": self.view, "label": LABEL, "base_object": "Lead", "is_standard": 1})
		self.assertFalse(self._row().is_standard, "upsert must not publish a view")
		smartview.set_public(self.view, 1)
		self.assertTrue(frappe.db.get_value("CRM Smart View", self.view, "is_standard"))
		smartview.set_public(self.view, 0)

	def test_editing_does_not_hand_the_view_to_the_editor(self):
		"""Ownership is set once. Rewriting it on every save gave the view to whoever last touched it."""
		owner_before = self._row().owner_user
		smartview.upsert_view({"name": self.view, "label": LABEL + " edited", "base_object": "Lead"})
		self.assertEqual(self._row().owner_user, owner_before)
		smartview.upsert_view({"name": self.view, "label": LABEL, "base_object": "Lead"})

	def test_an_activity_view_may_only_be_authored_on_an_entitled_type(self):
		"""A Lead view's grain is clamped on the way in; the activity type now is too."""
		outsider = frappe.get_all(
			"CRM Task Type", filters={"vertical": ["not in", ["", partner_fixture.VERTICAL]]},
			pluck="name", limit=1,
		)
		if not outsider:
			self.skipTest("this bench carries no task type outside the fixture grain")
		with patch.object(smartview.entitlement, "entitled_grains", return_value={(partner_fixture.VERTICAL, partner_fixture.GROUP, "")}):
			with self.assertRaises(frappe.PermissionError):
				smartview.upsert_view({
					"label": LABEL + " activity", "base_object": "Activity", "activity_type": outsider[0],
					"vertical": partner_fixture.VERTICAL, "group": partner_fixture.GROUP,
				})
