# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The send-time routing guard (plan section 2.3 / 3.3): `sends.send_whatsapp` must refuse to send a
template through an account that did not approve it. Two grains route to two different WATI accounts;
a template lives on one account only. The match case (lead's grain routes to the template's own
account) sends; the mismatch case (lead's grain routes to the OTHER account) raises before any adapter
call. Real Frappe engine as the oracle (S.6) - real routing rows, a real rule fired through the real
dispatcher for the through-the-dispatcher case; only the WATI adapter's outbound call is spied (never a
real WATI HTTP call, per the plan's mandate).
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import dispatcher, sends, versions
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.whatsapp import api as wati_api

_RUN_LOG = "CRM Automation Run Log"
_DT = "CRM Automation Rule"
_GRAIN_A = GRAINS[0]
_GRAIN_B = GRAINS[1]
_PREFIX = "RoutingGuard-"


def _make_lead(grain, **extra):
	payload = {
		"doctype": "CRM Lead", "first_name": "RoutingGuard", "lead_name": "RoutingGuard Probe", "status": "New",
		"custom_vertical": grain["vertical"], "custom_group": grain["group"],
		"custom_current_program": grain["program"], "mobile_no": "+919876500002",
		"email": "routing-guard-probe@example.invalid",
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _make_wati_account(name):
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": name, "status": "Active",
		"url": "https://live-mt-server.wati.io/000001", "token": "routing-guard-test-token",
		"custom_provider": "WATI",
	}).insert(ignore_permissions=True).name


def _make_template(template_name, account):
	"""`WhatsApp Templates` is WATI-mirrored and read-only (WATITemplates.validate blocks a manual
	insert) - rows only ever land via `templates_sync`'s `db_insert`. Same seam as test_sends_gated.py."""
	full_name = f"{template_name}-en"
	if frappe.db.exists("WhatsApp Templates", full_name):
		frappe.delete_doc("WhatsApp Templates", full_name, force=True, ignore_permissions=True)
	doc = frappe.new_doc("WhatsApp Templates")
	doc.update({
		"template_name": template_name, "template": "<p>Hi</p>", "language_code": "en",
		"category": "UTILITY", "whatsapp_account": account, "actual_name": template_name,
		"status": "APPROVED",
	})
	doc.name = full_name
	doc.db_insert()
	return doc.name


def _make_routing(grain, account):
	key = f"{grain['vertical']}::{grain['group']}::{grain['program']}"
	if frappe.db.exists("CRM WhatsApp Routing", key):
		frappe.delete_doc("CRM WhatsApp Routing", key, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "CRM WhatsApp Routing", "vertical": grain["vertical"],
		"psp_group": grain["group"], "program": grain["program"], "whatsapp_account": account,
	}).insert(ignore_permissions=True).name


def _cleanup(prefix):
	frappe.db.delete("CRM Automation Action", {"parent": ("like", f"{prefix}%")})
	frappe.db.delete(_DT, {"rule_name": ("like", f"{prefix}%")})
	frappe.db.delete(_RUN_LOG, {"rule": ("like", f"{prefix}%")})


class TestSendWhatsappRoutingGuard(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.account_a = _make_wati_account("RoutingGuard-account-a")
		cls.account_b = _make_wati_account("RoutingGuard-account-b")
		cls.routing_a = _make_routing(_GRAIN_A, cls.account_a)
		cls.routing_b = _make_routing(_GRAIN_B, cls.account_b)
		cls.template_on_a = _make_template("RoutingGuard-template", cls.account_a)
		cls.lead_on_a = _make_lead(_GRAIN_A)
		cls.lead_on_b = _make_lead(_GRAIN_B)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Lead", {"name": ("in", [cls.lead_on_a.name, cls.lead_on_b.name])})
		frappe.delete_doc("CRM WhatsApp Routing", cls.routing_a, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM WhatsApp Routing", cls.routing_b, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Templates", cls.template_on_a, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.account_a, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.account_b, force=True, ignore_permissions=True)

	def _flip_on(self):
		"""Test-scoped monkeypatch, not a DB write - same idiom as test_sends_gated.py."""
		orig_sends, orig_is_enabled = sends.sends_enabled, wati_api.is_enabled
		sends.sends_enabled = lambda: True
		wati_api.is_enabled = lambda: True
		return orig_sends, orig_is_enabled

	def _spy_adapter(self):
		calls = []
		orig_send = wati_api.send_template_message

		def _fake_send(account, **kwargs):
			calls.append((account, kwargs))
			return {"result": True, "local_message_id": "wamid-routing-guard-test"}

		wati_api.send_template_message = _fake_send
		return calls, orig_send

	def test_match_sends_once_through_the_templates_own_account(self):
		orig_sends, orig_is_enabled = self._flip_on()
		calls, orig_send = self._spy_adapter()
		try:
			result = sends.send_whatsapp(self.lead_on_a.name, self.template_on_a, {})
		finally:
			wati_api.send_template_message = orig_send
			sends.sends_enabled = orig_sends
			wati_api.is_enabled = orig_is_enabled
		self.assertTrue(result.startswith("sent:"), f"expected a 'sent: ...' marker, got {result!r}")
		self.assertEqual(len(calls), 1, "the adapter must be called exactly once on a match")
		account, kwargs = calls[0]
		self.assertEqual(account.name, self.account_a)
		self.assertEqual(kwargs["template_name"], "RoutingGuard-template")

	def test_mismatch_raises_and_never_calls_the_adapter(self):
		"""Lead routes to account B; the template belongs to account A - fail-closed."""
		orig_sends, orig_is_enabled = self._flip_on()
		calls, orig_send = self._spy_adapter()
		try:
			with self.assertRaises(ValueError):
				sends.send_whatsapp(self.lead_on_b.name, self.template_on_a, {})
		finally:
			wati_api.send_template_message = orig_send
			sends.sends_enabled = orig_sends
			wati_api.is_enabled = orig_is_enabled
		self.assertEqual(calls, [], "mismatch must never reach the adapter (fail-closed)")

	def test_through_the_dispatcher_mismatch_writes_failed_run_log_and_error_log(self):
		"""Simulates 'routing changed after the rule was authored' (plan section 1, row 3): the rule is
		authored with only the Product Line set - too partial to pin a single account, so
		`_validate_send_whatsapp` allows the save (msgprint, not throw) - and the lead that actually
		fires it carries the full grain that routes to account B, mismatching the picked template."""
		orig_sends, orig_is_enabled = self._flip_on()
		calls, orig_send = self._spy_adapter()
		rule = frappe.get_doc({
			"doctype": _DT, "rule_name": f"{_PREFIX}mismatch-rule", "enabled": 1,
			"on_doctype": "CRM Lead", "event": "Updated", "vertical": _GRAIN_B["vertical"],
			"actions": [{"action_type": "Send WhatsApp", "whatsapp_template": self.template_on_a}],
		}).insert(ignore_permissions=True)
		try:
			axes = (_GRAIN_B["vertical"], _GRAIN_B["group"], _GRAIN_B["program"])
			outcome = dispatcher.run_effects(
				self.lead_on_b.name, versions.current_name(rule.name), self.lead_on_b, axes, "grain", {}, {},
			)
			self.assertTrue(outcome.failed, "a mismatched Send WhatsApp action must fail the segment")
			self.assertEqual(calls, [], "the dispatcher must not reach the adapter on a mismatch")
			logs = frappe.get_all(
				_RUN_LOG, filters={"rule": rule.name}, fields=["outcome", "error"],
			)
			self.assertTrue(logs, "no Run Log row written for the failed fire")
			self.assertEqual(logs[0].outcome, "Failed")
			errors = frappe.get_all(
				"Error Log",
				filters={"method": "automation: rule fire failed", "error": ["like", f"%rule={rule.name}%"]},
				fields=["name"],
			)
			self.assertTrue(errors, "no Error Log row titled 'automation: rule fire failed' was written")
		finally:
			wati_api.send_template_message = orig_send
			sends.sends_enabled = orig_sends
			wati_api.is_enabled = orig_is_enabled
			_cleanup(_PREFIX)


if __name__ == "__main__":
	unittest.main()
