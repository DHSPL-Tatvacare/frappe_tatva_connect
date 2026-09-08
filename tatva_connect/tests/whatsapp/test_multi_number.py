# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE ACCOUNT, MANY NUMBERS — a send names the number it leaves from, and inbound refuses another's.

A WATI account carries up to 25 WhatsApp numbers behind ONE url and ONE token. Every send we made
named none of them, so WATI used the account's DEFAULT number: on a shared account a patient in one
programme would have been messaged from another brand's number, with WATI answering success. Inbound
had the mirror hole — one webhook can serve several of an account's numbers, and the payload's
`channelPhoneNumber` was read by nothing.

WHERE THE PARAMETER GOES WAS MEASURED, NOT READ. WATI's own multi-number article puts it in the
payload and is wrong twice over. Live 4-way probes (body and query × both of WATI's spellings) showed:

  * v1 session text -> `channelPhoneNumber` was honoured only in the QUERY, never in the body their
    doc prescribes, where it is accepted, answered `ok: true`, and ignored.
  * v1 template ---> honours NONE of the four spellings.

Both are moot now: every send speaks v3, where a conversation is named by a CHANNEL-SCOPED TARGET
(`<channel>:<contact>`) and a template by its own `channel` field. One addressing rule, measured — the
bare contact returned 41 messages across two numbers, the scoped target the 16 that were its own.

So the assertions below are not a reading of the documentation; they are the shape that was seen to
work on a real handset, and each one goes red if the wire drifts back to the shape that silently lost.

TWO LOCKS, AND THE FIRST ONE MATTERS MOST. `test_a_single_number_account_*` pins that an account with
one number names NO channel — neither a key nor an empty one, which WATI would read as a number it
cannot find — so it keeps sending from the only number it has.

DRIVEN AT THE ENTRY POINT. The sends go through `wati.send_*` — the functions `message.py`,
`notification.py` and `automation.sends` all call — and the assertion is made on the wire underneath.

Hermetic: no network (the session / multipart post is intercepted), no lead and no routing rule.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.whatsapp.test_multi_number
"""
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.whatsapp import transport, wati

# One account url for both rows — exactly how GoodFlip's numbers share one WATI subscription.
_TENANT = "https://live-mt-server.wati.io/000077"
_SINGLE = "Multinumber-single-account"
_MULTI = "Multinumber-multi-account"
_SINGLE_NUMBER = "919900000771"
_MULTI_NUMBER = "919900000772"
_ANOTHER_NUMBER = "919900000773"
_PATIENT = "919900000001"
_TEMPLATE = frappe._dict(actual_name="a_template", template_name="a_template")


def _account(name, number, multi):
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": name, "status": "Active",
		"url": _TENANT, "token": "multi-number-test-token", "custom_provider": "WATI",
		"custom_wati_channel_number": number, "custom_wati_multi_number": 1 if multi else 0,
	}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input


class _Wire:
	"""Intercepts the one HTTP call a send makes, and hands back the URL and body it built."""

	def __init__(self, answer=None):
		answered = mock.Mock()
		answered.json.return_value = answer if answer is not None else {"result": True}
		self.session = mock.Mock()
		self.session.request.return_value = answered

	@property
	def url(self):
		return self.session.request.call_args.args[1]

	@property
	def body(self):
		return self.session.request.call_args.kwargs["json"]

	@property
	def content_type(self):
		return self.session.request.call_args.kwargs["headers"]["Content-Type"]

	def __enter__(self):
		self._patch = mock.patch.object(transport, "get_request_session", return_value=self.session)
		self._patch.start()
		return self

	def __exit__(self, *exc):
		self._patch.stop()


# What WATI really answered to a v3 CONVERSATION send — text, media and media-by-url share one envelope.
_CONV_OK = {"message": {"local_message_id": "echoed-id", "id": "wati-id", "status": "sent"}}

# What WATI really answered to a v3 template send, trimmed to the fields that decide the outcome.
_V3_OK = {
	"success": True,
	"broadcast_id": "6a9fca34493af38917e8e7d8",
	"recipients": [{"local_message_id": "echoed-id", "phone_number": _PATIENT, "target": _PATIENT, "errors": []}],
}


class TestMultiNumberAccount(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.single = _account(_SINGLE, _SINGLE_NUMBER, multi=False)
		cls.multi = _account(_MULTI, _MULTI_NUMBER, multi=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		for name in (_SINGLE, _MULTI):
			if frappe.db.exists("WhatsApp Account", name):
				frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	# ===================================================================== The no-regression lock =====================================================================
	def test_a_single_number_account_names_no_channel_at_all(self):
		"""An empty `channel` is not the same as no `channel`: WATI would read one as a number it cannot
		find. An account with one number must send the key nowhere and leave from the number it has."""
		with _Wire(_V3_OK) as wire:
			wati.send_template(self.single, _PATIENT, _TEMPLATE)
		self.assertNotIn("channel", wire.body, "a single-number account named a channel")
		self.assertEqual(wire.body["template_name"], "a_template")
		self.assertEqual(wire.body["recipients"][0]["phone_number"], _PATIENT)

	def test_a_single_number_account_addresses_the_bare_contact(self):
		"""One number means the contact holds one conversation, so the bare contact is a true target and
		nothing extra reaches the wire."""
		with _Wire(_CONV_OK) as wire:
			wati.send_session(self.single, _PATIENT, "hello")
		self.assertEqual(wire.body, {"target": _PATIENT, "text": "hello"})

	def test_the_adapter_names_no_number_for_a_single_number_account(self):
		self.assertEqual(wati._channel_number(self.single), "")

	# ===================================================================== Session text — the channel-scoped target =====================================================================
	def test_a_session_send_names_its_number_in_the_target(self):
		"""Unscoped, this reaches the CONTACT, whose thread on a shared account carries every number's
		messages — and the send leaves from the account's default number."""
		with _Wire(_CONV_OK) as wire:
			wati.send_session(self.multi, _PATIENT, "hello")
		self.assertEqual(wire.body["target"], f"{_MULTI_NUMBER}:{_PATIENT}")
		self.assertIn("/api/ext/v3/conversations/messages/text", wire.url)

	# ===================================================================== Templates — v3, because v1 cannot name a number =====================================================================
	def test_a_multi_number_template_send_goes_to_v3_with_the_channel_named(self):
		"""v1 honoured neither of WATI's spellings in either position; v3's `channel` is the only one
		that reached the right number."""
		with _Wire(_V3_OK) as wire:
			wati.send_template(self.multi, _PATIENT, _TEMPLATE)
		self.assertIn("/api/ext/v3/messageTemplates/send", wire.url)
		self.assertEqual(wire.body["channel"], _MULTI_NUMBER)
		self.assertEqual(wire.body["template_name"], "a_template")

	def test_the_v3_send_is_host_rooted_and_carries_no_tenant_segment(self):
		"""Every v3 call is host-rooted; built on the v1 tenant path WATI answers 404."""
		with _Wire(_V3_OK) as wire:
			wati.send_template(self.multi, _PATIENT, _TEMPLATE)
		self.assertTrue(wire.url.startswith("https://live-mt-server.wati.io/api/ext/v3/"), wire.url)
		self.assertNotIn("000077", wire.url)

	def test_the_v3_send_uses_plain_json_not_the_v1_content_type(self):
		with _Wire(_V3_OK) as wire:
			wati.send_template(self.multi, _PATIENT, _TEMPLATE)
		self.assertEqual(wire.content_type, "application/json")

	def test_the_v3_send_carries_one_recipient_with_a_correlation_id_we_minted(self):
		"""v1 mints the id and hands it back; v3 takes ours. Every delivery tick joins on it, so the
		send must carry one rather than leaving the message with no identity to confirm."""
		with _Wire(_V3_OK) as wire:
			wati.send_template(self.multi, _PATIENT, _TEMPLATE)
		recipients = wire.body["recipients"]
		self.assertEqual(len(recipients), 1)
		self.assertEqual(recipients[0]["phone_number"], _PATIENT)
		self.assertTrue(recipients[0].get("local_message_id"), "the send named no correlation id")

	def test_the_correlation_id_stored_is_the_one_wati_echoed(self):
		"""Read the ECHO, never our own variable — an id WATI never acknowledged is an id no status
		event will ever mention."""
		with _Wire(_V3_OK):
			result = wati.send_template(self.multi, _PATIENT, _TEMPLATE)
		self.assertTrue(result.accepted)
		self.assertEqual(result.correlation_id, "echoed-id")

	# ===================================================================== The v3 envelope, classified =====================================================================
	def test_a_v3_refusal_against_the_one_recipient_is_a_refusal(self):
		"""`success: true` describes the REQUEST. A per-recipient error is the message not being sent,
		and reading only the request flag would record it as delivered."""
		result = wati._classify({
			"success": True, "broadcast_id": "b1",
			"recipients": [{"local_message_id": "x", "errors": ["invalid phone number"]}],
		})
		self.assertFalse(result.accepted)
		self.assertIn("invalid phone number", result.error or "")

	def test_a_v3_response_with_no_recipient_is_not_a_success(self):
		result = wati._classify({"success": True, "broadcast_id": "b1", "recipients": []})
		self.assertFalse(result.accepted)

	def test_the_conversation_envelope_is_classified_on_its_own_terms(self):
		"""Two envelopes, one result. The broadcast answers per recipient; a conversation answers with one
		message and mints the id itself, so nothing downstream can tell which endpoint replied."""
		result = wati._classify(_CONV_OK)
		self.assertTrue(result.accepted)
		self.assertEqual(result.correlation_id, "echoed-id")

	def test_a_conversation_send_the_provider_marks_failed_is_a_refusal(self):
		"""An accepted CALL is not an accepted MESSAGE: the request answers 200 either way."""
		refused = wati._classify(
			{"message": {"local_message_id": "x", "status": "failed", "failed_detail": "no session"}}
		)
		self.assertFalse(refused.accepted)
		self.assertIn("no session", refused.error)

	def test_an_envelope_this_provider_no_longer_sends_is_refused_not_guessed(self):
		"""The v1 shapes are gone from the wire. Reading one as success would record a message nobody
		can confirm was sent, so anything unrecognised is a refusal carrying what it said."""
		self.assertFalse(wati._classify({"result": True, "message": {"localMessageId": "v1-id"}}).accepted)
		self.assertFalse(wati._classify({"result": False, "info": "no credits"}).accepted)
		self.assertEqual(wati._classify({"result": False, "info": "no credits"}).error, "no credits")

	# ===================================================================== Media =====================================================================
	def test_a_media_send_names_its_number_in_the_target(self):
		"""Multipart, so the target rides as a form field beside the bytes rather than as JSON."""
		answered = mock.Mock()
		answered.json.return_value = _CONV_OK
		with mock.patch.object(transport.requests, "post", return_value=answered) as posted:
			wati.send_media(self.multi, _PATIENT, "f.pdf", b"x", "application/pdf")
		self.assertEqual(posted.call_args.kwargs["data"]["target"], f"{_MULTI_NUMBER}:{_PATIENT}")
		self.assertIn("/api/ext/v3/conversations/messages/file", posted.call_args.args[0])

	def test_a_media_url_send_names_its_number_in_the_target(self):
		with _Wire(_CONV_OK) as wire:
			wati.send_media_url(self.multi, _PATIENT, "https://e.example/f.pdf")
		self.assertEqual(wire.body["target"], f"{_MULTI_NUMBER}:{_PATIENT}")
		self.assertEqual(wire.body["file_url"], "https://e.example/f.pdf")

	# ===================================================================== Every send path, not only the ones above =====================================================================
	def test_the_adapter_names_the_number_on_every_send_it_offers(self):
		"""The lock that survives a new send path: each adapter send is really driven, and what it
		handed the wire is read off the transport call — a path added without the number goes red."""
		for wire_name, send in (
			("send_template_message", lambda: wati.send_template(self.multi, _PATIENT, _TEMPLATE)),
			("send_session_message", lambda: wati.send_session(self.multi, _PATIENT, "hi")),
			("send_session_file", lambda: wati.send_media(self.multi, _PATIENT, "f.pdf", b"x", "application/pdf")),
			("send_session_file_via_url", lambda: wati.send_media_url(self.multi, _PATIENT, "https://e.example/f")),
		):
			with self.subTest(wire=wire_name), mock.patch.object(
				transport, wire_name, return_value=_CONV_OK
			) as sent:
				send()
				self.assertEqual(
					sent.call_args.kwargs.get("channel_number"), _MULTI_NUMBER,
					f"{wire_name} was sent without naming the number it leaves from",
				)

	# ===================================================================== Inbound — an event that landed on another number =====================================================================
	def test_an_event_received_on_another_number_of_the_account_is_not_ingested(self):
		"""One webhook can serve several of an account's numbers. Registered against the wrong one,
		every reply to that brand would have been filed under this account's leads."""
		wanted, reason = wati.screen(
			{"eventType": "message", "owner": False, "waId": _PATIENT, "channelPhoneNumber": _ANOTHER_NUMBER},
			account=_MULTI,
		)
		self.assertFalse(wanted)
		self.assertIn(_ANOTHER_NUMBER, reason)

	def test_an_event_received_on_this_rows_own_number_is_never_dropped_for_the_number(self):
		"""Spelling is not identity: `+919...` and `919...` are the same number."""
		_wanted, reason = wati.screen(
			{"eventType": "message", "owner": False, "waId": _PATIENT, "channelPhoneNumber": f"+{_MULTI_NUMBER}"},
			account=_MULTI,
		)
		self.assertNotIn("not this account's", reason)

	def test_a_payload_that_names_no_channel_is_screened_exactly_as_before(self):
		"""Every payload from the single-number account in production is this shape."""
		_wanted, reason = wati.screen(
			{"eventType": "message", "owner": False, "waId": _PATIENT}, account=_MULTI
		)
		self.assertNotIn("not this account's", reason)
