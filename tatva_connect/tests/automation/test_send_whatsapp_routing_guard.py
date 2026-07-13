# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The send-time routing guard (plan section 2.3 / 3.3), rewritten to the DEFERRED send model (R1,
post-audit remediation, plan section 8b): `sends.send_whatsapp` validates and resolves SYNCHRONOUSLY
(so a mismatch or bad config still fails the segment before anything is queued) and, on a match,
RETURNS a thunk instead of sending inline. No test performs a real WATI send or a real background job
run: `frappe.enqueue` is always spied at the call site (proving the deferred contract itself), and the
actual delivery function `sends._deliver_whatsapp` is tested directly with a spied adapter. Real Frappe
engine as the oracle (S.6) - real routing rows, a real rule fired through the real dispatcher for the
through-the-dispatcher cases.
"""
import hashlib
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import dispatcher, sends, versions
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.whatsapp import api as wati_api

_RUN_LOG = "CRM Automation Run Log"
_DT = "CRM Automation Rule"
_FIELD = field_allowlist.DOCTYPE
_GRAIN_A = GRAINS[0]
# The two grains must differ by product line + group, not only by program: both leads carry ONE number, and `ix_lead_dedup_unique` is (mobile, vertical, group). Two programs inside one group cannot hold the same patient — the case this suite proves is one patient enrolled in two businesses.
_GRAIN_B = GRAINS[2]
_AXES_A = (_GRAIN_A["vertical"], _GRAIN_A["group"], _GRAIN_A["program"])
_PREFIX = "RoutingGuard-"
_DELIVER_METHOD = "tatva_connect.automation.sends._deliver_whatsapp"


def _make_lead(grain, **extra):
	payload = {
		"doctype": "CRM Lead", "first_name": "RoutingGuard", "lead_name": "RoutingGuard Probe", "status": "New",
		"custom_vertical": grain["vertical"], "custom_group": grain["group"],
		"custom_current_program": grain["program"], "mobile_no": "+919876500002",
		"email": "routing-guard-probe@example.invalid",
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)



def _channel_number(name):
	"""A distinct WABA number per account — the field is unique."""
	return f"9190{int(hashlib.md5(name.encode()).hexdigest(), 16) % 10**8:08d}"

def _make_wati_account(name):
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": name, "status": "Active",
		"url": "https://live-mt-server.wati.io/000001", "token": "routing-guard-test-token",
		"custom_provider": "WATI", "custom_wati_channel_number": _channel_number(name),
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

	def _spy_enqueue(self):
		"""Spy `frappe.enqueue` itself - the deferred contract is what these tests prove, so nothing
		here ever runs a real background job or a real WATI HTTP call."""
		calls = []
		orig = frappe.enqueue

		def spy(method, **kwargs):
			calls.append((method, kwargs))

		frappe.enqueue = spy
		return calls, orig

	def _spy_adapter(self):
		calls = []
		orig_send = wati_api.send_template_message

		def _fake_send(account, **kwargs):
			calls.append((account, kwargs))
			return {"result": True, "local_message_id": "wamid-routing-guard-test"}

		wati_api.send_template_message = _fake_send
		return calls, orig_send

	def test_match_returns_deferred_thunk_that_enqueues_once_when_called(self):
		orig_sends, orig_is_enabled = self._flip_on()
		enqueue_calls, orig_enqueue = self._spy_enqueue()
		try:
			result = sends.send_whatsapp(self.lead_on_a.name, self.template_on_a, {})
			self.assertTrue(callable(result), "a match must return a deferred thunk, not send inline")
			self.assertEqual(enqueue_calls, [], "nothing may enqueue until the thunk is actually invoked")
			result()
		finally:
			sends.sends_enabled = orig_sends
			wati_api.is_enabled = orig_is_enabled
			frappe.enqueue = orig_enqueue
		self.assertEqual(len(enqueue_calls), 1, "the thunk must enqueue exactly once when invoked")
		method, kwargs = enqueue_calls[0]
		self.assertEqual(method, _DELIVER_METHOD)
		self.assertTrue(kwargs.get("enqueue_after_commit"), "the delivery job must be enqueued after_commit")
		self.assertEqual(kwargs["account_name"], self.account_a)
		self.assertEqual(kwargs["template_name"], "RoutingGuard-template")

	def test_mismatch_raises_and_never_enqueues(self):
		"""Lead routes to account B; the template belongs to account A - fail-closed."""
		orig_sends, orig_is_enabled = self._flip_on()
		enqueue_calls, orig_enqueue = self._spy_enqueue()
		try:
			with self.assertRaises(ValueError):
				sends.send_whatsapp(self.lead_on_b.name, self.template_on_a, {})
		finally:
			sends.sends_enabled = orig_sends
			wati_api.is_enabled = orig_is_enabled
			frappe.enqueue = orig_enqueue
		self.assertEqual(enqueue_calls, [], "mismatch must never reach frappe.enqueue (fail-closed)")

	def test_through_the_dispatcher_mismatch_writes_failed_run_log_and_never_enqueues(self):
		"""Simulates 'routing changed after the rule was authored' (plan section 1, row 3): the rule is
		authored with only the Product Line set - too partial to pin a single account, so
		`_validate_send_whatsapp` allows the save (msgprint, not throw) - and the lead that actually
		fires it carries the full grain that routes to account B, mismatching the picked template."""
		orig_sends, orig_is_enabled = self._flip_on()
		enqueue_calls, orig_enqueue = self._spy_enqueue()
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
			self.assertEqual(enqueue_calls, [], "the dispatcher must never reach frappe.enqueue on a mismatch")
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
			sends.sends_enabled = orig_sends
			wati_api.is_enabled = orig_is_enabled
			frappe.enqueue = orig_enqueue
			_cleanup(_PREFIX)

	def test_a_later_sibling_action_failing_clears_the_deferred_send(self):
		"""Finding-1 regression (plan section 8b, R1): a rule [Send WhatsApp (valid, matching), Update
		Field (fails at RUNTIME)] must roll back the WHOLE segment, including the already-queued Send
		WhatsApp thunk - `dispatcher.run_effects` clears `deferred` on any exception (dispatcher.py
		~line 148) before it ever runs the deferred list, so nothing enqueues despite the send having
		validated cleanly. Same known-bad shape as test_effect_verbs's Call Webhook rollback test:
		author the 2nd action's field WHILE allowlisted (so the rule passes validate at save time), then
		disable the allowlist row before firing - the runtime recheck (defense in depth) fails it."""
		bad_field = "custom_patient_age"
		allow_row = field_allowlist.seed_settable(
			"CRM Lead", bad_field, _GRAIN_A["vertical"], _GRAIN_A["group"], _GRAIN_A["program"]
		)
		rule = frappe.get_doc({
			"doctype": _DT, "rule_name": f"{_PREFIX}sibling-fails-rule", "enabled": 1,
			"on_doctype": "CRM Lead", "event": "Updated",
			"vertical": _GRAIN_A["vertical"], "group": _GRAIN_A["group"], "program": _GRAIN_A["program"],
			"actions": [
				{"action_type": "Send WhatsApp", "whatsapp_template": self.template_on_a},
				{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": bad_field,
				 "value_mode": "Literal", "value": "40"},
			],
		}).insert(ignore_permissions=True)
		frappe.db.set_value(_FIELD, allow_row, "enabled", 0)  # stale: authoring permitted it, runtime must not
		orig_sends, orig_is_enabled = self._flip_on()
		enqueue_calls, orig_enqueue = self._spy_enqueue()
		# Also spy the adapter directly - an inline-send mutation (the pre-R1 bug) never reaches
		# frappe.enqueue at all, so enqueue_calls alone would read empty either way. Only a direct
		# adapter spy actually distinguishes "deferred, then cleared on rollback" from "sent inline
		# before the sibling even had a chance to fail" - see the mutation-proof note in the plan.
		adapter_calls, orig_send = self._spy_adapter()
		try:
			outcome = dispatcher.run_effects(
				self.lead_on_a.name, versions.current_name(rule.name), self.lead_on_a, _AXES_A, "grain", {}, {},
			)
			self.assertTrue(outcome.failed, "the sibling's runtime failure must fail the whole segment")
			self.assertEqual(
				enqueue_calls, [],
				"the Send WhatsApp thunk enqueued despite the segment rolling back - a patient would be "
				"messaged while the Run Log records Failed",
			)
			self.assertEqual(
				adapter_calls, [],
				"the WATI adapter was called despite the segment rolling back - the send fired inline "
				"instead of being deferred past the commit",
			)
			logs = frappe.get_all(_RUN_LOG, filters={"rule": rule.name}, fields=["outcome"])
			self.assertTrue(logs, "no Run Log row written for the failed fire")
			self.assertEqual(logs[0].outcome, "Failed")
		finally:
			sends.sends_enabled = orig_sends
			wati_api.is_enabled = orig_is_enabled
			frappe.enqueue = orig_enqueue
			wati_api.send_template_message = orig_send
			frappe.db.delete(_FIELD, {"name": allow_row})
			_cleanup(_PREFIX)

	def test_deliver_whatsapp_calls_the_adapter_with_the_resolved_args(self):
		"""`_deliver_whatsapp` is the job body `enqueue_after_commit` runs - call it directly (as the
		job runner would) and prove it calls the adapter with exactly what was queued."""
		calls, orig_send = self._spy_adapter()
		try:
			sends._deliver_whatsapp(
				account_name=self.account_a,
				to_number="919876500002",
				template_name="RoutingGuard-template",
				parameters=[{"name": "1", "value": "RoutingGuard"}],
				lead=self.lead_on_a.name,
			)
		finally:
			wati_api.send_template_message = orig_send
		self.assertEqual(len(calls), 1, "the adapter must be called exactly once")
		account, kwargs = calls[0]
		self.assertEqual(account.name, self.account_a)
		self.assertEqual(kwargs["to_number"], "919876500002")
		self.assertEqual(kwargs["template_name"], "RoutingGuard-template")
		self.assertEqual(kwargs["parameters"], [{"name": "1", "value": "RoutingGuard"}])


if __name__ == "__main__":
	unittest.main()
