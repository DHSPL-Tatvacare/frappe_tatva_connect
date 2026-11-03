# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A grain that keeps outgoing calls to the lead's own reps credits nobody else, except a holder of a role it exempts."""
import random

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.telephony.adapters import acefone
from tatva_connect.tests.telephony.fixtures import config

DID = "9000100003"
REP = "_test_credit_rep@example.com"
SUPPORT = "_test_credit_support@example.com"
SEATS = {REP: "0600000000101", SUPPORT: "0600000000102"}
EXEMPT = "_Test Telephony Exempt"


class TestCreditRelatedOnly(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		config.ensure_account(rep_emails=tuple(SEATS))
		if not frappe.db.exists("Role", EXEMPT):
			frappe.get_doc({"doctype": "Role", "role_name": EXEMPT, "desk_access": 1}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture
		frappe.get_doc("User", SUPPORT).add_roles(EXEMPT)
		for user, seat in SEATS.items():
			config.set_seat(user, seat)
		cls.addClassCleanup(config.clear_seats)
		cls.addClassCleanup(config.set_rules, config.current_rules())

	def setUp(self):
		config.set_rules([config.rule("Outbound", "Any")])
		config.clear_dids()
		config.map_did(DID)
		self.addCleanup(config.clear_dids)
		config.credit_related_only(True, exempt_roles=(EXEMPT,))
		self.lead = config.lead(f"9{random.randint(100_000_000, 999_999_999)}")
		self.addCleanup(frappe.delete_doc, "CRM Lead", self.lead.name, force=True, ignore_permissions=True)

	def _dialled_by(self, user):
		"""One answered outgoing call from this grain's DID to the lead, placed on the user's own seat."""
		answered = next(r for r in config.corpus() if r.get("call_status") == "answered")
		payload = {**answered, "call_id": frappe.generate_hash(length=16), "direction": "Dialer (outbound)",
		           "caller_id_number": DID, "call_to_number": self.lead.mobile_no[3:],
		           "customer_number_with_prefix": self.lead.mobile_no, "answered_agent_number": SEATS[user]}
		name = acefone.process(payload, event="outbound_complete", account=config.ACCOUNT)
		return frappe.db.get_value("CRM Call Log", name, ["reference_docname", "caller"], as_dict=True)

	def test_a_rep_calling_a_lead_they_do_not_hold_lands_uncredited(self):
		row = self._dialled_by(REP)
		self.assertEqual(row.reference_docname, self.lead.name)
		self.assertFalse(row.caller)

	def test_an_exempt_role_is_credited_on_the_same_lead(self):
		row = self._dialled_by(SUPPORT)
		self.assertEqual(row.reference_docname, self.lead.name)
		self.assertEqual(row.caller, SUPPORT)
