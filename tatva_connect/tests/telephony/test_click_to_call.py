# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Click-to-call from a lead: who may place it, and the provider's hangup completing the row it created. Only Acefone's HTTP is stubbed.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.telephony.test_click_to_call
"""
import random
from unittest.mock import patch

import requests

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.telephony import api, bridge
from tatva_connect.telephony.adapters import acefone
from tatva_connect.tests.telephony.fixtures import config

CALLER_ID = "9000100001"  # the account's caller ID, which the patient sees
SEAT = "0600000000911"
OWNER = "_test_tel_owner@example.com"


class TestClickToCall(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		config.ensure_account(rep_emails=(OWNER,))
		config.ensure_account(account=config.SIBLING_ACCOUNT, token=f"{config.ACCOUNT}-token")
		cls.addClassCleanup(config.set_rules, config.current_rules())

	def setUp(self):
		config.set_rules([config.rule("Outbound", "Dialer")])
		config.clear_dids()
		config.clear_seats()
		config.clear_seats(config.SIBLING_ACCOUNT)
		config.map_did(CALLER_ID)
		config.set_seat("Administrator", SEAT)
		self.addCleanup(config.switch_on(api.SWITCH_CALLS))
		self.digits = f"9{random.randint(100_000_000, 999_999_999)}"

	def tearDown(self):
		config.credit_related_only(0)
		config.clear_dids()
		config.clear_seats()
		config.clear_seats(config.SIBLING_ACCOUNT)

	def _lead(self, owner):
		lead = config.lead(self.digits, owner=owner)
		self.addCleanup(frappe.delete_doc, "CRM Lead", lead.name, force=True, ignore_permissions=True)
		return lead

	def _calls(self, lead):
		return frappe.get_all("CRM Call Log", filters={"reference_docname": lead.name}, fields=["name", "status", "caller"])

	def test_with_the_switch_on_a_rep_who_is_not_on_the_lead_cannot_call_it(self):
		lead = self._lead(owner=OWNER)
		config.credit_related_only(1)
		with patch.object(api, "make_post_request") as acefone_http:
			with self.assertRaisesRegex(frappe.ValidationError, "owner or an assignee"):
				bridge.make_a_call(self.digits, "CRM Lead", lead.name)
		acefone_http.assert_not_called()
		self.assertEqual(self._calls(lead), [])

	def test_the_providers_hangup_completes_the_row_the_call_created(self):
		lead = self._lead(owner="Administrator")
		config.credit_related_only(1)
		ref_id = frappe.generate_hash(length=20)
		with patch.object(api, "make_post_request", return_value={"success": True, "ref_id": ref_id}):
			bridge.make_a_call(self.digits, "CRM Lead", lead.name)
		self.assertEqual([(c.status, c.caller) for c in self._calls(lead)], [("Initiated", "Administrator")])

		acefone.process({  # the live click-to-call hangup body (2026-09-01), echoing Acefone's ref_id
			"call_id": frappe.generate_hash(length=16), "direction": "clicktocall", "call_status": "answered",
			"caller_id_number": CALLER_ID, "call_to_number": f"91{self.digits}", "ref_id": ref_id,
			"answered_agent": {"id": f"05{SEAT[2:]}", "name": "Rep-Extension", "number": SEAT}, "answered_agent_number": SEAT,
		}, event="outbound_complete", account=config.ACCOUNT)
		self.assertEqual([(c.status, c.caller) for c in self._calls(lead)], [("Completed", "Administrator")])

	def test_no_answer_from_the_provider_is_not_a_failure_and_holds_a_redial(self):
		lead = self._lead(owner="Administrator")
		with patch.object(api, "make_post_request", side_effect=requests.exceptions.ReadTimeout("Read timed out")):
			with self.assertRaisesRegex(frappe.ValidationError, "did not answer in time"):
				bridge.make_a_call(self.digits, "CRM Lead", lead.name)
		self.assertEqual([c.status for c in self._calls(lead)], ["Initiated"])
		with patch.object(api, "make_post_request") as acefone_http:
			with self.assertRaisesRegex(frappe.ValidationError, "already calling"):
				bridge.make_a_call(self.digits, "CRM Lead", lead.name)
		acefone_http.assert_not_called()
