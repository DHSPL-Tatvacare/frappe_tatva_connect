# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A saved view is scoped to ONE grain, because its columns are one grain's columns.

A view's rows are leads; its columns are a fixed set for the whole table. Resolve those columns against
several grains at once and the table carries grain A's columns beside grain B's leads — structurally
blank for most rows, and contradicting the rule the Data Tab states in its own docstring: *"an Anaya
lead never shows TatvaPractice fields even for an admin entitled to every grain"*.

`_grains_from_axes` unions the caller's entitled grains when a view names none. That is right for the
EDITOR — before a grain is picked, the picker must offer everything the caller could pick — and wrong
for a SAVED view, where the choice has been made and the table has to mean something.

So the grain is settled at save:
  * hold exactly one  -> it is yours, nothing to choose, it is filled in
  * hold several      -> name the one this view is for
  * System Manager    -> may leave it open; ALL_GRAINS is the whole catalog by design

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_a_view_is_scoped_to_one_grain
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.smartview import api as smartview

VERTICAL = "ZZ One Grain Vertical"
GROUP_ONE = "ZZ One Grain Group One"
GROUP_TWO = "ZZ One Grain Group Two"
USER = "zz-one-grain@example.com"


class TestAViewIsScopedToOneGrain(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("CRM Vertical", VERTICAL):
			frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": VERTICAL}).insert(ignore_permissions=True)
		for group in (GROUP_ONE, GROUP_TWO):
			if not frappe.db.exists("CRM Group", group):
				frappe.get_doc({"doctype": "CRM Group", "group_name": group}).insert(ignore_permissions=True)
		if not frappe.db.exists("User", USER):
			user = frappe.get_doc({
				"doctype": "User", "email": USER, "first_name": "One Grain",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
			user.append("roles", {"role": "Sales User"})
			user.save(ignore_permissions=True)
		cls.one = (VERTICAL, GROUP_ONE, "")
		cls.two = (VERTICAL, GROUP_TWO, "")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge_views()
		for dt, name in (("User", USER), ("CRM Group", GROUP_ONE), ("CRM Group", GROUP_TWO),
		                 ("CRM Vertical", VERTICAL)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@staticmethod
	def _purge_views():
		for name in frappe.get_all("CRM Smart View", filters={"label": ["like", "ZZ One Grain%"]}, pluck="name"):
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self._purge_views()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge_views()
		frappe.db.commit()

	def _save(self, label, grains, axes=None):
		frappe.set_user(USER)
		try:
			with patch.object(entitlement, "entitled_grains", return_value=grains):
				return smartview.upsert_view({"label": label, "base_object": "Lead", **(axes or {})})["name"]
		finally:
			frappe.set_user("Administrator")

	def test_a_multi_grain_caller_must_name_the_grain(self):
		"""THE rule. Two grains and no choice made is a table that cannot mean one thing."""
		with self.assertRaises(frappe.ValidationError):
			self._save("ZZ One Grain Unscoped", {self.one, self.two})

	def test_a_multi_grain_caller_may_name_it(self):
		"""And having named it, the view is scoped to exactly that."""
		name = self._save("ZZ One Grain Named", {self.one, self.two},
		                  {"vertical": VERTICAL, "group": GROUP_ONE})
		doc = frappe.get_doc("CRM Smart View", name)
		self.assertEqual((doc.vertical, doc.group), (VERTICAL, GROUP_ONE))

	def test_a_single_grain_caller_has_it_filled_in(self):
		"""Nothing to choose, so nothing is asked — the stored view still carries the grain, because the
		read path must not have to re-derive it later from who happens to be looking."""
		name = self._save("ZZ One Grain Sole", {self.one})
		doc = frappe.get_doc("CRM Smart View", name)
		self.assertEqual(
			(doc.vertical, doc.group), (VERTICAL, GROUP_ONE),
			"a caller with one grain must get it stamped on the view, not left blank",
		)

	def test_a_system_manager_may_leave_it_open(self):
		"""ALL_GRAINS is the whole catalog by design; an admin's cross-grain view is deliberate."""
		frappe.set_user("Administrator")
		name = smartview.upsert_view({"label": "ZZ One Grain Admin", "base_object": "Lead", "is_standard": 1})["name"]
		doc = frappe.get_doc("CRM Smart View", name)
		self.assertFalse(doc.vertical or doc.group or doc.program)
