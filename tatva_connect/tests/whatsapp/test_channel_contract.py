# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The CHANNEL contract — what stops WhatsApp from being WATI-shaped again.

The characterisation suite next door pins BEHAVIOUR. This one pins the SHAPE: that a channel is keyed
by channel, that an adapter declares what it can truthfully do, that a canonical event never carries a
vendor's name, and that the four defects with no behavioural fixture of their own stay fixed.

Every test here failed on the pre-refactor code — the defect each one covers is named in its docstring.

Hermetic: no network. The one send test mocks the HTTP call and asserts only the URL that was built.
"""
import unittest
from typing import ClassVar
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.channels import contract, resolve
from tatva_connect.channels import event as channel_event
from tatva_connect.webhooks import registry
from tatva_connect.whatsapp import channel, transport, wati

_ACCOUNT = "Contract-test-account"


def _account(url):
	if frappe.db.exists("WhatsApp Account", _ACCOUNT):
		frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": _ACCOUNT, "status": "Active",
		"url": url, "token": "contract-test-token", "custom_provider": "WATI",
	}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input


class TestChannelContract(FrappeTestCase):
	@classmethod
	def tearDownClass(cls):
		if frappe.db.exists("WhatsApp Account", _ACCOUNT):
			frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()

	# ============================================================================= The declaration =============================================================================
	def test_wati_declares_itself_as_one_provider_on_the_whatsapp_channel(self):
		self.assertEqual(wati.DECLARATION.channel, "whatsapp")
		self.assertEqual(wati.DECLARATION.provider, "WATI")
		self.assertEqual(wati.DECLARATION.account_doctype, "WhatsApp Account")

	# The send-side function a capability PROMISES. Declaring one without it is the lie the field exists to prevent — `buttons` was declared for three weeks with no builder anywhere in the app.
	_CAPABILITY_IMPLEMENTATION: ClassVar[dict] = {
		"templates": "send_template",
		"media": "send_media",
		"session": "send_session",
		"backfill": "history",
		"recover_message": "recover_message",
		"recover_media": "fetch_media_by_message_id",
	}

	def test_every_capability_wati_declares_has_a_function_behind_it(self):
		"""Not a restatement of the declaration — the declaration checked against the module.

		This replaces an assertion of the capability set against a hardcoded copy of itself, which would
		have passed had all seven capabilities been fiction. One of them was.
		"""
		for capability in wati.DECLARATION.capabilities:
			fn = self._CAPABILITY_IMPLEMENTATION.get(capability)
			self.assertIsNotNone(fn, f"{capability} is declared but this test knows no function for it")
			self.assertTrue(callable(getattr(wati, fn, None)), f"{capability} declared, {fn}() missing")

	def test_wati_declares_no_capability_it_cannot_exercise(self):
		"""The other direction: a send-side capability with no builder must not be declared at all.

		`buttons` and `lists` both arrive INBOUND and are read — a tap becomes `clicked`. Reading one is
		not offering it, and neither has a send-side builder, so neither may be claimed.
		"""
		for unbuildable in ("buttons", "lists"):
			self.assertNotIn(unbuildable, wati.DECLARATION.capabilities)

	def test_an_adapter_cannot_declare_an_outcome_the_vocabulary_does_not_have(self):
		"""A provider that could claim an outcome nobody defined could claim anything at all, and a
		consumer downstream would believe it."""
		with self.assertRaises(ValueError):
			contract.declare(
				channel="whatsapp", provider="Imaginary", account_doctype="WhatsApp Account",
				outcomes={"telepathed"}, capabilities=set(), number_format=contract.E164_PLAIN,
			)
		with self.assertRaises(ValueError):
			contract.declare(
				channel="whatsapp", provider="Imaginary", account_doctype="WhatsApp Account",
				outcomes=set(), capabilities={"mind-reading"}, number_format=contract.E164_PLAIN,
			)

	def test_every_outcome_wati_declares_is_one_it_can_actually_emit(self):
		"""The declaration is a promise. Anything the event map produces must be inside it, or the
		declaration is decoration."""
		for outcome in wati.STATUS_BY_EVENT.values():
			self.assertTrue(wati.DECLARATION.emits(outcome), outcome)

	# ============================================================================= The registry — keyed by channel, no vendor in the route (defect 11 / §2) =============================================================================
	def test_the_registry_is_keyed_by_channel_not_by_vendor(self):
		self.assertIn("whatsapp", registry.CHANNELS)
		self.assertNotIn("WATI", registry.CHANNELS)
		self.assertEqual(registry.providers_for("whatsapp"), ["WATI"])

	def test_the_whatsapp_webhook_url_carries_no_vendor_segment(self):
		"""Defect: the route was /webhooks/whatsapp/wati/<token>. A tenant moving vendor would have had
		to re-register a URL on a dashboard we do not control — so the vendor is out of the path and the
		token, which already identifies the account, is the whole address."""
		cfg = registry.by_channel("whatsapp")
		urls = [t["url"] for t in cfg["targets"]("https://example.test", frappe._dict(), "TOK")]
		self.assertEqual(urls, ["https://example.test/webhooks/whatsapp/TOK"])
		self.assertNotIn("wati", urls[0].lower())

	def test_telephony_still_resolves_through_the_same_one_registry(self):
		"""Acefone must not regress: it is a second channel in the SAME declaration, not a survivor of
		the old vendor-keyed one."""
		cfg = registry.by_channel("telephony")
		self.assertEqual(cfg["account_doctype"], "CRM Telephony Account")
		self.assertEqual(registry.providers_for("telephony"), ["Acefone"])
		self.assertIs(registry.by_account_doctype("CRM Telephony Account"), cfg)

	def test_the_adapter_is_resolved_from_the_accounts_own_provider_field(self):
		account = _account("https://live-mt-server.wati.io/000000")
		self.assertIs(resolve.adapter_for(account), wati)
		self.assertTrue(resolve.has_adapter(account))

	def test_an_account_naming_an_unregistered_provider_fails_loud(self):
		"""No silent default: a misconfigured account must raise rather than send through whichever
		adapter happened to be first."""
		account = _account("https://live-mt-server.wati.io/000000")
		account.custom_provider = "Gupshup"
		self.assertFalse(resolve.has_adapter(account))
		with self.assertRaises(frappe.exceptions.ValidationError):
			resolve.adapter_for(account)

	# ============================================================================= The canonical event =============================================================================
	def test_an_emitted_event_name_never_carries_the_vendor(self):
		"""The whole point of the layer. A consumer that keys on a vendor breaks the day it changes."""
		for case, outcome in (("sentMessageDELIVERED_v2", "delivered"), ("sentMessageREPLIED_v2", "replied")):
			ev = channel_event.build(
				channel="whatsapp", provider="WATI", account=None, kind="status",
				outcome=outcome, correlation_id="lmid-1",
			)
			name = channel_event.event_name(ev)
			self.assertEqual(name, f"whatsapp.{outcome}", case)
			self.assertNotIn("wati", name.lower())

	def test_a_status_event_without_a_correlation_id_is_refused(self):
		"""A status that cannot name the message it is about can tick nothing — building one anyway is
		how an event looks handled and is not."""
		with self.assertRaises(ValueError):
			channel_event.build(
				channel="whatsapp", provider="WATI", account=None, kind="status", outcome="read",
			)

	def test_an_event_cannot_be_edited_after_it_is_built(self):
		"""It is a fact about something that already happened. Every bug where one was rewritten in
		flight — a status quietly changed between the screen and the write — is one this cannot have."""
		ev = channel_event.build(
			channel="whatsapp", provider="WATI", account=None, kind="status",
			outcome="read", correlation_id="lmid-1",
		)
		with self.assertRaises(TypeError):
			ev["outcome"] = "delivered"
		with self.assertRaises(TypeError):
			ev.update({"outcome": "delivered"})
		self.assertEqual(ev.outcome, "read")

	def test_an_unknown_kind_or_outcome_is_refused(self):
		with self.assertRaises(ValueError):
			channel_event.build(channel="whatsapp", provider="WATI", account=None, kind="telepathy")
		with self.assertRaises(ValueError):
			channel_event.build(
				channel="whatsapp", provider="WATI", account=None, kind="inbound", outcome="pondered",
			)

	def test_normalize_carries_the_button_identity_and_the_reply_context(self):
		"""Defect 2, at the event boundary: the id, the title and the tapped message's context all have
		somewhere to live before anything is persisted."""
		ev = wati.normalize({
			"eventType": "message", "owner": False, "waId": "919900000001", "type": "interactive",
			"text": "Test Button", "id": "wati-1", "whatsappMessageId": "wamid-1",
			"interactiveButtonReply": {"id": "btn_yes", "title": "Test Button"},
			"replyContextId": "wamid-outbound-1",
		}, None)
		self.assertEqual(ev.kind, "inbound")
		self.assertEqual(ev.outcome, "clicked")
		self.assertEqual(ev.button_id, "btn_yes")
		self.assertEqual(ev.button_title, "Test Button")
		self.assertEqual(ev.reply_to, "wamid-outbound-1")

	def test_normalize_returns_nothing_for_a_payload_that_is_not_ours(self):
		self.assertIsNone(wati.normalize({"eventType": "probe"}, None))
		self.assertIsNone(wati.normalize({}, None))

	# ============================================================================= Defect 3 — a trailing slash on the account URL 404s every call =============================================================================
	def test_a_trailing_slash_on_the_account_url_does_not_double_up(self):
		"""Defect 3: `https://host/360078/` built `https://host/360078//api/v1/...`, WATI answered 404 to
		EVERY call, and nothing said why. One rstrip, at the one place the base is used."""
		self.assertEqual(
			transport.base_url(frappe._dict(url="https://live-mt-server.wati.io/000000/")),
			"https://live-mt-server.wati.io/000000",
		)
		self.assertEqual(
			transport.base_url(frappe._dict(url="https://live-mt-server.wati.io/000000")),
			"https://live-mt-server.wati.io/000000",
		)

	def test_a_trailing_slash_account_builds_a_reachable_send_url(self):
		"""The same defect where it actually bit: the composed endpoint, not just the base.

		The sends are host-rooted v3 now, so the tenant segment a stray slash used to double is not even
		in the URL — but the defect is asserted where it still can happen, on the v1 media route the
		provider's own webhook payloads point at."""
		account = _account("https://live-mt-server.wati.io/000000/")
		answered = mock.Mock()
		answered.json.return_value = {"message": {"local_message_id": "x", "status": "sent"}}
		session = mock.Mock()
		session.request.return_value = answered
		with mock.patch.object(transport, "get_request_session", return_value=session):
			transport.send_session_message(account, "919900000001", "hello")
		url = session.request.call_args.args[1]
		self.assertNotIn("//api", url)
		self.assertTrue(url.startswith("https://live-mt-server.wati.io/api/ext/v3/"), url)
		# The tenant-scoped base, where a pasted slash still composes a URL the provider 404s.
		self.assertEqual(
			transport.base_url(account, transport.API_V1), "https://live-mt-server.wati.io/000000"
		)

	# ============================================================================= Defect 4 — a correlation id no status event will ever echo =============================================================================
	def test_a_send_never_stores_an_id_the_status_events_do_not_echo(self):
		"""Defect 4: `whatsappMessageId` used to be accepted as a fallback correlation id. Every WATI
		status event names the localMessageId and nothing else, so a message stored under the other id
		was a message whose delivered / read / failed never arrived — for ever."""
		result = wati._classify({
			"message": {"local_message_id": None, "whatsapp_message_id": "wamid.SHOULD-NEVER-BE-STORED",
			            "status": "sent"},
		})
		self.assertTrue(result.accepted)
		self.assertIsNone(result.correlation_id)
		self.assertEqual(result.wamid, "wamid.SHOULD-NEVER-BE-STORED", "the wamid is kept as a SECOND id")

	def test_a_send_stores_the_local_message_id_the_status_events_do_echo(self):
		"""The defect is unchanged; the envelopes are the ones the provider sends today. A broadcast
		answers per recipient and a conversation answers with one message, and the id read out of either
		is the one every status event will echo — a row stored under any other id is never ticked."""
		for resp, expected in (
			({"success": True, "recipients": [{"local_message_id": "lmid-a", "errors": []}]}, "lmid-a"),
			({"message": {"local_message_id": "lmid-b", "status": "sent"}}, "lmid-b"),
		):
			with self.subTest(resp=resp):
				self.assertEqual(wati._classify(resp).correlation_id, expected)

	def test_a_failed_send_is_reported_with_its_reason_and_no_id(self):
		"""Both refusal shapes: a recipient the provider named an error against, and the `info` the
		transport normalises an unreadable or 4xx body into."""
		refused = wati._classify(
			{"success": True, "recipients": [{"local_message_id": "x", "errors": ["out of credits"]}]}
		)
		self.assertFalse(refused.accepted)
		self.assertIsNone(refused.correlation_id)
		self.assertIn("out of credits", refused.error)

		unreadable = wati._classify({"result": False, "info": "out of credits"})
		self.assertFalse(unreadable.accepted)
		self.assertIsNone(unreadable.correlation_id)
		self.assertEqual(unreadable.error, "out of credits")

	# ============================================================================= Defect 11 / §3 — no vendor gates, no vendor switch keys =============================================================================
	def test_the_switch_keys_carry_no_vendor(self):
		"""Defect §3: keyed `WhatsApp::WATI::messaging`, an operator changing provider would have gone
		dark on the migrate that introduced the new key — it defaults OFF, and nothing would have said
		why. The vendor's own control is the account's Active/Inactive status."""
		for key in (channel.SWITCH_MESSAGING, channel.SWITCH_TEMPLATES, channel.SWITCH_RECONCILE, channel.SWITCH_RECOVERY):
			self.assertNotIn("WATI", key)
			self.assertTrue(key.startswith("WhatsApp::"), key)
			self.assertTrue(
				frappe.db.exists("CRM Tatva Automation", key),
				f"{key} must exist as a row, or the switch gates nothing",
			)

	def test_the_switches_are_all_dormant_by_default(self):
		"""Constitution: every automation ships OFF. The rekey must not have flipped one on."""
		for key in (channel.SWITCH_MESSAGING, channel.SWITCH_TEMPLATES, channel.SWITCH_RECONCILE, channel.SWITCH_RECOVERY):
			self.assertFalse(
				frappe.db.get_value("CRM Tatva Automation", key, "enabled"),
				f"{key} must ship dormant",
			)

	def test_the_dead_vendor_gates_are_gone(self):
		"""Defect 11: `assert_wati`, `is_wati_account` and `WATI_HOST_MARKER` guessed a vendor from a URL
		host substring and refused anything else. Provider identity is the account's own field now, and
		a URL host is a deployment detail — so the gates are deleted, not renamed."""
		for gone in ("assert_wati", "is_wati_account", "WATI_HOST_MARKER"):
			self.assertFalse(hasattr(wati, gone), gone)
			self.assertFalse(hasattr(transport, gone), gone)
			self.assertFalse(hasattr(channel, gone), gone)

	def test_no_module_named_after_the_vendor_survives_as_the_channels_brain(self):
		"""The old second registry (`whatsapp.providers`) and the old vendor-shaped adapter
		(`whatsapp.adapter`) are deleted, not aliased. Two ways to do one thing is the failure mode."""
		for dotted in ("tatva_connect.whatsapp.providers", "tatva_connect.whatsapp.adapter",
		               "tatva_connect.whatsapp.api"):
			with self.assertRaises(ImportError, msg=dotted):
				__import__(dotted)


if __name__ == "__main__":
	unittest.main()
