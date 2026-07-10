# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 7 — the dormant sends gate. `Send WhatsApp` / `Send Email` fire through the SAME two-lane
effect executor as every other verb (`dispatcher.run_effects` -> `actions._ACTION_LANES`), but every
outbound call sits behind `Task::Automation::sends` (OFF by default, A.6): the rule still fires end
to end and the Run Log records the intent, but the adapter/native mailer is never invoked until the
switch is on. Real Frappe engine as the oracle (S.6) — a real rule, a real Run Log row, and a real
grain-routed WhatsApp Account/Routing/Template; only the adapter's outbound call and `frappe.sendmail`
are spied (never a mocked verdict on the gate/routing logic itself). The switch itself is flipped via
a test-scoped monkeypatch of `sends.sends_enabled`, never a persisted DB write (brief, Part D) — the
crux is that dormant-by-default suppresses for real, not that a test remembered to reset a row.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, dispatcher, sends, versions
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.whatsapp import api as wati_api

_RUN_LOG = "CRM Automation Run Log"
_DT = "CRM Automation Rule"
_GRAIN = GRAINS[0]
_AXES = (_GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])


def _action_row(**fields):
	"""A duck-typed action row - handlers read attributes off it (same shape as a real
	`CRM Automation Action` child row) - same helper shape as test_effect_verbs.py."""
	class _A:
		pass
	a = _A()
	for k, v in fields.items():
		setattr(a, k, v)
	return a


def _make_lead(**extra):
	payload = {
		"doctype": "CRM Lead", "first_name": "SendsGate", "lead_name": "SendsGate Probe", "status": "New",
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"], "mobile_no": "+919876500001",
		"email": "sendsgate-probe@example.invalid",
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _make_wati_account(name):
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": name, "status": "Active",
		"url": "https://live-mt-server.wati.io/000000", "token": "sends-gate-test-token",
		"custom_provider": "WATI",
	}).insert(ignore_permissions=True).name


def _make_template(template_name, account):
	"""`WhatsApp Templates` is a WATI-mirrored READ-ONLY doctype (manual `.insert()` is blocked by
	`WATITemplates.validate()`, tatva_connect/whatsapp/templates.py) — rows only ever land via
	`templates_sync`'s `db_insert` (bypasses the Meta-bound validate). Same seam here."""
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


def _make_routing(account):
	key = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}"
	if frappe.db.exists("CRM WhatsApp Routing", key):
		frappe.delete_doc("CRM WhatsApp Routing", key, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "CRM WhatsApp Routing", "vertical": _GRAIN["vertical"],
		"psp_group": _GRAIN["group"], "program": _GRAIN["program"], "whatsapp_account": account,
	}).insert(ignore_permissions=True).name


def _make_rule(name, action_rows):
	return frappe.get_doc({
		"doctype": _DT, "rule_name": name, "enabled": 1,
		"on_doctype": "CRM Lead", "event": "Updated",
		"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
		"actions": action_rows,
	}).insert(ignore_permissions=True)


def _cleanup(prefix):
	frappe.db.delete("CRM Automation Action", {"parent": ("like", f"{prefix}%")})
	frappe.db.delete(_DT, {"rule_name": ("like", f"{prefix}%")})
	frappe.db.delete(_RUN_LOG, {"rule": ("like", f"{prefix}%")})


class TestSendsGateOffSuppressesBoth(FrappeTestCase):
	"""The crux (Part D): switch OFF (default — no persisted flip, this class never touches the DB
	row), a rule with BOTH Send verbs fires end-to-end through the real executor, the Run Log records
	`suppressed`, and — the point of the whole gate — neither adapter is ever invoked."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.lead = _make_lead()
		cls.account = _make_wati_account("SendsGate-off-account")
		cls.template = _make_template("SendsGate-off-template", cls.account)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Lead", {"name": cls.lead.name})
		frappe.delete_doc("WhatsApp Templates", cls.template, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.account, force=True, ignore_permissions=True)

	def test_switch_off_by_default(self):
		self.assertFalse(sends.sends_enabled(), "Task::Automation::sends must be dormant by default (A.6)")

	def test_off_switch_suppresses_and_adapters_never_called(self):
		rule = _make_rule("SendsGate-off-rule", [
			{"action_type": "Send WhatsApp", "whatsapp_template": self.template},
			{"action_type": "Send Email", "email_recipient": "patient@example.invalid",
			 "email_subject": "Welcome", "email_body": "Hello there"},
		])
		wa_calls, mail_calls = [], []
		orig_send, orig_mail = wati_api.send_template_message, frappe.sendmail
		wati_api.send_template_message = lambda *a, **kw: wa_calls.append((a, kw))
		frappe.sendmail = lambda **kw: mail_calls.append(kw)
		try:
			dispatcher.run_effects(
				self.lead.name, versions.current_name(rule.name), self.lead, _AXES, "grain", {}, {},
			)
			logs = frappe.get_all(_RUN_LOG, filters={"rule": rule.name}, fields=["outcome", "details", "actions_failed"])
			self.assertTrue(logs, "no Run Log row written for the fire")
			self.assertEqual(logs[0].outcome, "Success")
			self.assertEqual(logs[0].actions_failed, 0)
			self.assertIn("suppressed", logs[0].details, "Run Log did not record the sends-dormant marker")
			self.assertEqual(wa_calls, [], "WATI adapter was called despite Task::Automation::sends being OFF")
			self.assertEqual(mail_calls, [], "frappe.sendmail was called despite Task::Automation::sends being OFF")
		finally:
			wati_api.send_template_message = orig_send
			frappe.sendmail = orig_mail
			_cleanup("SendsGate-off-rule")


class TestSendsGateOnCallsRealBrain(FrappeTestCase):
	"""Switch ON (test-scoped monkeypatch of `sends.sends_enabled` — never a persisted flip): the
	SAME handlers now call the real WATI/notification brain — grain-routed account resolution via
	`whatsapp.routing`, the existing adapter surface — and native `frappe.sendmail`. Only the outbound
	network/mail call is stubbed; routing + gate logic run for real."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.lead = _make_lead()
		cls.account = _make_wati_account("SendsGate-on-account")
		cls.template = _make_template("SendsGate-on-template", cls.account)
		cls.routing = _make_routing(cls.account)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Lead", {"name": cls.lead.name})
		frappe.delete_doc("CRM WhatsApp Routing", cls.routing, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Templates", cls.template, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.account, force=True, ignore_permissions=True)

	def _flip_on(self):
		"""Test-scoped monkeypatch, not a DB write - restores in the caller's finally."""
		orig_sends, orig_is_enabled = sends.sends_enabled, wati_api.is_enabled
		sends.sends_enabled = lambda: True
		wati_api.is_enabled = lambda: True  # the separate WATI master kill-switch (A.11), also dormant by default
		return orig_sends, orig_is_enabled

	def test_send_whatsapp_returns_deferred_thunk_that_enqueues_the_grain_routed_account(self):
		"""R1 (post-audit remediation): Send WhatsApp rides the transaction now - it returns a deferred
		thunk instead of calling the adapter inline. Spy `frappe.enqueue` (never the adapter directly
		here; test_send_whatsapp_routing_guard.py's `_deliver_whatsapp` test covers the adapter call)
		and invoke the thunk, same contract as Call Webhook."""
		orig_sends, orig_is_enabled = self._flip_on()
		enqueue_calls = []
		orig_enqueue = frappe.enqueue

		def _spy_enqueue(method, **kwargs):
			enqueue_calls.append((method, kwargs))

		frappe.enqueue = _spy_enqueue
		try:
			a = _action_row(action_type="Send WhatsApp", whatsapp_template=self.template)
			result = actions._action_send_whatsapp(a, self.lead.name, {}, _AXES, self.lead)
			self.assertTrue(callable(result), "a match must return a deferred thunk, not send inline")
			self.assertEqual(enqueue_calls, [], "nothing may enqueue until the thunk is actually invoked")
			result()
		finally:
			frappe.enqueue = orig_enqueue
			sends.sends_enabled = orig_sends
			wati_api.is_enabled = orig_is_enabled
		self.assertEqual(len(enqueue_calls), 1, "the send must enqueue exactly once when the gate is on")
		method, kwargs = enqueue_calls[0]
		self.assertEqual(method, "tatva_connect.automation.sends._deliver_whatsapp")
		self.assertEqual(kwargs["account_name"], self.account, "the grain-routed account was not the one WhatsApp Routing points to")
		self.assertEqual(kwargs["to_number"], wati_api.normalize_number("+919876500001"))

	def test_send_email_calls_sendmail_once(self):
		orig_sends, orig_is_enabled = self._flip_on()
		calls = []
		orig_mail = frappe.sendmail
		frappe.sendmail = lambda **kw: calls.append(kw)
		try:
			a = _action_row(
				action_type="Send Email", email_recipient="patient@example.invalid",
				email_subject="Welcome", email_body="Hello there",
			)
			result = actions._action_send_email(a, self.lead.name, {}, _AXES, self.lead)
		finally:
			frappe.sendmail = orig_mail
			sends.sends_enabled = orig_sends
			wati_api.is_enabled = orig_is_enabled
		self.assertEqual(len(calls), 1, "frappe.sendmail must be called exactly once when the gate is on")
		self.assertEqual(calls[0]["recipients"], ["patient@example.invalid"])
		self.assertEqual(calls[0]["subject"], "Welcome")
		self.assertTrue(result.startswith("queued:"), f"expected a 'queued: ...' marker, got {result!r}")


class TestSendsGatePlantedBad(FrappeTestCase):
	"""Planted-bad (S.6): a blank template/recipient/subject/body is a misconfiguration, not "nothing
	to send" - it must RAISE, in EITHER gate state (never a silent no-op that just looks the same as
	suppressed)."""

	def test_blank_whatsapp_template_raises_when_dormant(self):
		self.assertFalse(sends.sends_enabled())
		a = _action_row(action_type="Send WhatsApp", whatsapp_template=None)
		with self.assertRaises(ValueError):
			actions._action_send_whatsapp(a, "does-not-matter", {}, _AXES, None)

	def test_blank_whatsapp_template_raises_when_live(self):
		orig_sends = sends.sends_enabled
		sends.sends_enabled = lambda: True
		try:
			a = _action_row(action_type="Send WhatsApp", whatsapp_template="")
			with self.assertRaises(ValueError):
				actions._action_send_whatsapp(a, "does-not-matter", {}, _AXES, None)
		finally:
			sends.sends_enabled = orig_sends

	def test_blank_email_recipient_raises(self):
		a = _action_row(action_type="Send Email", email_recipient=None, email_subject="x", email_body="y")
		with self.assertRaises(ValueError):
			actions._action_send_email(a, "does-not-matter", {}, _AXES, None)

	def test_blank_email_body_raises(self):
		a = _action_row(action_type="Send Email", email_recipient="a@example.invalid", email_subject="x", email_body="")
		with self.assertRaises(ValueError):
			actions._action_send_email(a, "does-not-matter", {}, _AXES, None)


if __name__ == "__main__":
	unittest.main()
