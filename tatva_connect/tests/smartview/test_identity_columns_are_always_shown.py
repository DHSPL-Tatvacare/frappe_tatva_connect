# Copyright (c) 2026, TatvaCare and Contributors. See license.txt
"""Every Lead view leads with the always-shown identity columns, catalog-bounded: a withheld key is dropped, never forced."""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as smartview
from tatva_connect.smartview import catalog


class TestIdentityColumnsArePinned(FrappeTestCase):
	def _cat(self):
		return catalog._catalog_fields("Lead", None, smartview.entitlement.ALL_GRAINS, frappe.get_roles())

	def test_a_saved_set_that_omits_them_still_carries_them(self):
		"""A saved set without identity columns still leads with them and keeps the author's choice."""
		cat = self._cat()
		always = catalog._always_shown("Lead", cat)
		self.assertTrue(always, "this bench catalogs no identity column to pin")
		keys = catalog._with_always_shown(["lead:custom_substage"], "Lead", cat)
		self.assertEqual(keys[: len(always)], list(always), "the identity columns lead the set")
		self.assertIn("lead:custom_substage", keys, "the author's own choice is kept")

	def test_they_are_not_duplicated_when_the_author_chose_them_too(self):
		cat = self._cat()
		always = list(catalog._always_shown("Lead", cat))
		keys = catalog._with_always_shown([always[0], "lead:custom_substage"], "Lead", cat)
		self.assertEqual(len(keys), len(set(keys)), "a chosen always-shown column must not appear twice")

	def test_an_always_shown_key_outside_the_callers_catalog_is_dropped_not_forced(self):
		"""Fail-closed: pinning may never widen what a caller sees."""
		narrowed = {k: v for k, v in self._cat().items() if k != "lead:mobile_no"}
		self.assertNotIn("lead:mobile_no", catalog._always_shown("Lead", narrowed))

	def test_an_activity_view_always_shows_nothing(self):
		"""An Activity view has no always-shown columns."""
		self.assertEqual(catalog._always_shown("Activity", self._cat()), ())

	def test_the_picker_is_told_the_same_answer_the_composer_projects(self):
		"""One declaration. A picker that offered to drop a column the read path puts back would be lying."""
		cat = self._cat()
		offered = {c["field_key"] for c in smartview.field_catalog("Lead") if c.get("always_shown")}
		self.assertEqual(offered, set(catalog._always_shown("Lead", cat)))

	def test_the_id_is_pinned_even_for_a_caller_granted_no_fields(self):
		"""Pinned means pinned: the ID survives a grain that grants nothing."""
		self.assertIn(catalog.LEAD_ID, [r.fieldname for r in catalog._catalog_fields("Lead", None, set(), frappe.get_roles()).values()])

	def test_every_lead_view_leads_with_one_pinned_id_chip(self):
		"""The ID is the first column and the only chip, even when the author projected the ID column too."""
		created = smartview.upsert_view({"label": "ZZ Pinned ID", "base_object": "Lead", "vertical": "Goodflip-Care", "group": "Anaya", "columns": ["lead:lead_id", "lead:mobile_no"]})
		name = created if isinstance(created, str) else created["name"]
		try:
			columns = smartview.get_data(name, page_size=1)["columns"]
			self.assertEqual(columns[0]["fieldname"], catalog.LEAD_ID)
			self.assertEqual(len([c for c in columns if c.get("identity")]), 1, "the ID is the only chip")
			self.assertEqual(sum(1 for c in columns if c["fieldname"] == catalog.LEAD_ID), 1, "the ID must not show twice")
		finally:
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)
