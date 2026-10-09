# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A call finds its lead by the number's stored form, matched exactly, at every place that attributes one: the telephony resolver, the Partner API call write, and crm's call-row label.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.telephony.test_a_call_finds_its_lead_by_stored_number
"""
import random
from unittest.mock import patch

import frappe
from crm.integrations.api import get_contact_by_phone_number
from frappe.tests import IntegrationTestCase

from tatva_connect.api import partner_call
from tatva_connect.telephony import resolve
from tatva_connect.telephony.adapters import acefone

GRAIN = {"vertical": "Goodflip", "psp_group": "India", "program": None}


def _cdr(caller, prefixed=""):
	"""An inbound Acefone CDR from `caller`, normalized as the webhook does."""
	return acefone.normalize({
		"call_id": f"zz-{random.randint(1, 10**9)}", "direction": "inbound", "call_status": "missed",
		"caller_id_number": caller, "customer_number_with_prefix": prefixed, "call_to_number": "9000007179",
	})


class TestACallFindsItsLeadByStoredNumber(IntegrationTestCase):
	def setUp(self):
		# A lead's number is stored canonical only while dedup is on, as it is in production.
		switch = frappe.get_doc("CRM Tatva Automation", "Lead::CRM Lead::dedup")
		if not switch.enabled:
			switch.enabled = 1
			switch.save(ignore_permissions=True)
			self.addCleanup(lambda: frappe.get_doc("CRM Tatva Automation", "Lead::CRM Lead::dedup").update({"enabled": 0}).save(ignore_permissions=True))
		self.digits = f"9{random.randint(100_000_000, 999_999_999)}"
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ phone probe", "mobile_no": self.digits,
			"custom_vertical": "Goodflip", "custom_group": "India",
		}).insert(ignore_permissions=True)
		self.assertEqual(self.lead.mobile_no, f"+91{self.digits}")  # stored canonical on save

	def _asked(self, call):
		"""The `mobile_no` filter every CRM Lead read was asked with while `call` ran."""
		with patch.object(frappe, "get_all", wraps=frappe.get_all) as spy:
			result = call()
		asked = [c.kwargs["filters"]["mobile_no"] for c in spy.call_args_list
		         if c.args and c.args[0] == "CRM Lead" and "mobile_no" in (c.kwargs.get("filters") or {})]
		return result, asked

	def test_the_resolver_finds_the_lead_in_its_grain_by_an_exact_number(self):
		found, asked = self._asked(lambda: resolve.lead_for(_cdr(self.digits), GRAIN))
		self.assertEqual(found, self.lead.name)
		self.assertEqual(asked, [f"+91{self.digits}"])

	def test_the_resolver_never_reaches_another_grain(self):
		self.assertIsNone(resolve.lead_for(_cdr(self.digits), {"vertical": "Tatvapractice", "psp_group": "India"}))

	def test_a_number_with_no_stored_form_finds_nothing_and_raises_nothing(self):
		self.assertIsNone(resolve.lead_for(_cdr("1234567890"), GRAIN))
		self.assertFalse(frappe.get_message_log())

	def test_a_partner_call_finds_the_lead_by_an_exact_number(self):
		data = {"direction": "Outbound", "to_number": f"+91 {self.digits[:5]} {self.digits[5:]}"}
		found, asked = self._asked(lambda: partner_call._attribute_lead(data, frappe._dict(vertical="Goodflip", crm_group="India"), False))
		self.assertEqual(found, self.lead.name)
		self.assertEqual(asked, [f"+91{self.digits}"])

	def test_a_foreign_number_finds_its_lead_through_the_resolver_the_partner_api_and_the_call_row(self):
		foreign = f"+1415555{random.randint(100, 199):04d}"
		lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ foreign probe", "mobile_no": foreign,
			"custom_vertical": "Goodflip", "custom_group": "India",
		}).insert(ignore_permissions=True)
		self.assertEqual(resolve.lead_for(_cdr(foreign[-10:], prefixed=foreign), GRAIN), lead.name)
		data = {"direction": "Inbound", "from_number": foreign}
		self.assertEqual(partner_call._attribute_lead(data, frappe._dict(vertical="Goodflip", crm_group="India"), False), lead.name)
		self.assertEqual(get_contact_by_phone_number(foreign).get("lead"), lead.name)

	def test_the_call_row_label_finds_the_lead_by_an_exact_number(self):
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as spy:
			found = get_contact_by_phone_number(self.digits)
		lead_reads = [str(c.args[0]) for c in spy.call_args_list if "`tabCRM Lead`" in str(c.args[0])]
		self.assertEqual(found.get("lead"), self.lead.name)
		self.assertTrue(lead_reads)
		self.assertFalse([q for q in lead_reads if "LIKE" in q.upper()], lead_reads)
