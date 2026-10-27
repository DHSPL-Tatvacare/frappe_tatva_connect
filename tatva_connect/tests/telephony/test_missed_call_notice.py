# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A patient's unanswered call tells the lead's rep once, as Acefone really sends it: one hangup that already says missed; a call a reconcile files from the past tells nobody."""
import random

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.lead import assignment
from tatva_connect.notifications import dispatch
from tatva_connect.telephony.adapters import acefone
from tatva_connect.tests.telephony.fixtures import config

DID = "9000100002"
REP = "_test_missed_call_rep@example.com"


class TestMissedCallNotice(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		config.ensure_account(rep_emails=(REP,))
		cls.addClassCleanup(config.set_rules, config.current_rules())

	def setUp(self):
		config.set_rules([config.rule("Inbound", "Any")])
		config.clear_dids()
		config.map_did(DID)
		self.addCleanup(config.clear_dids)
		self.lead = config.lead(f"9{random.randint(100_000_000, 999_999_999)}")
		self.addCleanup(frappe.delete_doc, "CRM Lead", self.lead.name, force=True, ignore_permissions=True)
		assignment.assign("CRM Lead", self.lead.name, REP, notify=False, ignore_permissions=True)
		armed = dispatch.armed
		dispatch.armed = lambda: True  # the one switch, flipped for this test only, never written
		self.addCleanup(setattr, dispatch, "armed", armed)

	def _hangup(self):
		"""One anonymised live inbound missed hangup, re-pointed at this test's DID and lead."""
		missed = next(r for r in config.corpus() if r.get("direction") == "inbound" and r.get("call_status") == "missed")
		return {**missed, "call_id": frappe.generate_hash(length=16), "call_to_number": DID, "did_number": DID,
		        "caller_id_number": self.lead.mobile_no[3:], "customer_number_with_prefix": self.lead.mobile_no}

	def _told(self):
		return frappe.get_all("Notification Log", filters={"for_user": REP, "type": dispatch.MISSED_CALL,
		                                                   "document_name": self.lead.name}, pluck="name")

	def test_a_live_missed_call_tells_the_rep_once(self):
		name = acefone.process(self._hangup(), event="inbound_complete", account=config.ACCOUNT)
		self.assertEqual(frappe.db.get_value("CRM Call Log", name, "status"), "No Answer")
		self.assertEqual(len(self._told()), 1)

	def test_a_missed_call_filed_from_the_past_tells_nobody(self):
		frappe.flags.tatva_bulk_history = True  # as reconcile files a call pulled out of the past
		try:
			acefone.process(self._hangup(), event="inbound_complete", account=config.ACCOUNT)
		finally:
			frappe.flags.tatva_bulk_history = None
		self.assertEqual(self._told(), [])
