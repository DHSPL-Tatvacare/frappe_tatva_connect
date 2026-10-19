# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Merging folds one record into another through frappe's rename-merge, bringing across only what the person picks.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.merge.test_merge
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.helpdesk.contact import contact_for
from tatva_connect.merge.api import merge, preview


class TestMergeContacts(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_the_kept_contact_gains_the_phone_email_and_empty_fields_it_was_offered(self):
		keep = contact_for("+919812397031", full_name="Kept Person", create=True)
		drop = contact_for("+919812397032", full_name="Dropped Person", email="dropped.person@example.test", create=True)
		frappe.get_doc("Contact", drop).update({"last_name": "Rao"}).save()
		plan = preview("Contact", keep, drop)
		self.assertEqual({t["fieldname"] for t in plan["tables"]}, {"phone_nos", "email_ids"})
		merge("Contact", keep, drop, take=["last_name"], carry=["phone_nos", "email_ids"])
		kept = frappe.get_doc("Contact", keep)
		self.assertFalse(frappe.db.exists("Contact", drop))
		self.assertEqual(sorted(p.phone for p in kept.phone_nos), ["+919812397031", "+919812397032"])
		self.assertEqual([e.email_id for e in kept.email_ids], ["dropped.person@example.test"])
		self.assertEqual((kept.first_name, kept.last_name), ("Kept Person", "Rao"))

	def test_a_field_the_preview_did_not_offer_is_refused(self):
		keep = contact_for("+919812397033", create=True)
		drop = contact_for("+919812397034", create=True)
		with self.assertRaises(frappe.ValidationError):
			merge("Contact", keep, drop, take=["full_name"])
		self.assertTrue(frappe.db.exists("Contact", drop))


class TestMergeLeads(FrappeTestCase):
	def test_leads_of_different_grains_are_refused(self):
		from tatva_connect.taxonomy import grain

		seen = {}
		for name in frappe.get_all("CRM Lead", pluck="name", limit=500):
			seen.setdefault(grain.of("CRM Lead", name), name)
			if len(seen) == 2:
				break
		if len(seen) < 2:
			self.skipTest("this site holds leads of only one grain")
		keep, drop = seen.values()
		with self.assertRaisesRegex(frappe.ValidationError, "different programs"):
			preview("CRM Lead", keep, drop)
		# Desk's own Rename with "Merge with existing" meets the same gate.
		with self.assertRaisesRegex(frappe.ValidationError, "different programs"):
			frappe.rename_doc("CRM Lead", drop, keep, merge=True)
		frappe.db.rollback()
