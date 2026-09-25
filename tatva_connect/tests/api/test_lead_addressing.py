# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every lead endpoint addresses a lead by `name` or `mobile_no`; the `name` path is unchanged."""
import random
import unittest

import frappe

from tatva_connect.api import partner

VERTICAL, GROUP = "Goodflip-Care", "Anaya"


def _phone():
	return f"+919{random.randint(0, 10**9 - 1):09d}"


class TestLeadAddressing(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		self.sp = f"lead_addressing_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)
		self._form = frappe.form_dict

	def tearDown(self):
		frappe.form_dict = self._form
		try:
			frappe.db.rollback(save_point=self.sp)
		except Exception:
			frappe.db.rollback()  # an endpoint that refused rolled back the whole transaction; nothing to keep

	def _lead(self):
		"""A lead through the real upsert core, returned as (name, phone)."""
		phone = _phone()
		frappe.form_dict = frappe._dict(mobile_no=phone, first_name="Addressing Test",
		                                custom_vertical=VERTICAL, custom_group=GROUP)
		_u, mp, s, pf, ca = partner._caller_fields()
		doc, _action = partner._upsert_one(frappe.form_dict, mp, s, pf, ca, [])
		return doc.name, doc.mobile_no

	def _call(self, endpoint, **form):
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(form)
		endpoint()
		return frappe.local.response

	def test_update_by_mobile_no(self):
		name, phone = self._lead()
		body = self._call(partner.lead_update, mobile_no=phone, first_name="By Phone")
		self.assertEqual(body["status"], "success", body.get("error"))
		self.assertEqual(body["data"]["name"], name)
		self.assertEqual(frappe.db.get_value("CRM Lead", name, "first_name"), "By Phone")

	def test_update_by_name_is_unchanged(self):
		name, _phone_no = self._lead()
		body = self._call(partner.lead_update, name=name, first_name="By Name")
		self.assertEqual(body["data"]["name"], name)
		self.assertEqual(frappe.db.get_value("CRM Lead", name, "first_name"), "By Name")

	def test_update_bulk_by_mobile_no(self):
		name, phone = self._lead()
		body = self._call(partner.lead_update_bulk, updates=[{"mobile_no": phone, "first_name": "Bulk Phone"}])
		self.assertEqual(body["summary"]["succeeded"], 1, body.get("results"))
		self.assertEqual(frappe.db.get_value("CRM Lead", name, "first_name"), "Bulk Phone")

	def test_update_without_name_or_phone_is_refused(self):
		body = self._call(partner.lead_update, first_name="Nobody")
		self.assertEqual(body["error"]["code"], "validation_error")

	def test_delete_by_mobile_no(self):
		name, phone = self._lead()
		body = self._call(partner.lead_delete, mobile_no=phone)
		self.assertEqual(body["status"], "success", body.get("error"))
		self.assertEqual(body["data"]["name"], name, "the response names the lead that was deleted")
		self.assertFalse(frappe.db.exists("CRM Lead", name))

	def test_delete_bulk_by_mobile_nos(self):
		leads = [self._lead() for _ in range(2)]
		body = self._call(partner.lead_delete_bulk, mobile_nos=[phone for _n, phone in leads])
		self.assertEqual(body["summary"]["succeeded"], 2, body.get("results"))
		self.assertEqual([r["data"]["name"] for r in body["results"]], [n for n, _p in leads])
		self.assertFalse(any(frappe.db.exists("CRM Lead", n) for n, _p in leads))

	def test_delete_bulk_by_names_is_unchanged(self):
		name, _phone_no = self._lead()
		body = self._call(partner.lead_delete_bulk, names=[name])
		self.assertEqual(body["summary"]["succeeded"], 1, body.get("results"))
		self.assertFalse(frappe.db.exists("CRM Lead", name))

	def test_delete_bulk_without_a_key_is_still_refused(self):
		body = self._call(partner.lead_delete_bulk)
		self.assertEqual(body["error"]["code"], "validation_error")
