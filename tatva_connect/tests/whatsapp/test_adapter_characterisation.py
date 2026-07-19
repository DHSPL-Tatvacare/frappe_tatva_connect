# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""CHARACTERISATION suite for `tatva_connect.whatsapp.wati` — the WhatsApp inbound path.

Written before the generic-channel refactor to pin what the WATI-shaped adapter DID, so the refactor
could prove exactly what it changed. It has now served that purpose and been carried across: every
assertion is the same, except the eleven marked `# CHANGED <date>:` — each of which is one of the
defects the refactor was there to fix, and each of which failed on the old code first.

A `# CHANGED` note names the defect and the behaviour it replaced. An UNMARKED assertion that starts
failing is a regression, not a re-baseline.

The corpus (`fixtures/wati_corpus.jsonl`) is 16 real WATI webhook payloads harvested from the
site's `Integration Request` rows — one per distinct eventType, plus one per inbound message
subtype (text / image / document / interactive). Every identifying field is scrubbed: waId is
919900000001, senderName "Test User", operator identities are example.com, and the WATI ids /
wamids / media URLs are regenerated (the real wamid base64-encodes the patient's number).

Hermetic: this builds its own account / routing rule / lead and never touches the live
'WATI Anaya 360078' account. No network — the media download and the File layer are stubbed
(the real byte path is covered by tests/storage/test_file_layer_registry.py).
"""
import json
import os
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.whatsapp import media as media_module
from tatva_connect.whatsapp import transport
from tatva_connect.whatsapp import wati as adapter

_GRAIN = GRAINS[0]
_WA_ID = "919900000001"
_UNKNOWN_WA_ID = "919900000099"
_ROUTED_ACCOUNT = "Charac-routed-account"
_UNROUTED_ACCOUNT = "Charac-unrouted-account"

_CORPUS = os.path.join(os.path.dirname(__file__), "fixtures", "wati_corpus.jsonl")

# Every eventType the corpus carries, grouped by how `screen` classifies it today.
_INBOUND_CASES = ["message:text", "message:image", "message:document", "message:interactive"]
_SENT_V2_CASES = ["sessionMessageSent_v2", "templateMessageSent_v2"]
_STATUS_V2_CASES = [
	"sentMessageDELIVERED_v2", "sentMessageREAD_v2", "sentMessageREPLIED_v2", "templateMessageFailed",
]
_NOT_INGESTED_CASES = [
	"sessionMessageSent", "templateMessageSent", "sentMessageDELIVERED",
	"sentMessageREAD", "sentMessageREPLIED", "probe",
]


def _load_corpus():
	with open(_CORPUS) as f:
		return {json.loads(line)["case"]: json.loads(line)["payload"] for line in f if line.strip()}


CORPUS = _load_corpus()


def _payload(case, **overrides):
	"""A fresh copy of a corpus payload — tests mutate freely without leaking into the corpus."""
	p = dict(CORPUS[case])
	p.update(overrides)
	return p


def _account(name, channel):
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": name, "status": "Active",
		"url": "https://live-mt-server.wati.io/000000", "token": "charac-test-token",
		"custom_provider": "WATI", "custom_wati_channel_number": channel,
	}).insert(ignore_permissions=True).name


def _routing(grain, account):
	key = f"{grain['vertical']}::{grain['group']}::{grain['program']}"
	if frappe.db.exists("CRM WhatsApp Routing", key):
		frappe.delete_doc("CRM WhatsApp Routing", key, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "CRM WhatsApp Routing", "vertical": grain["vertical"],
		"psp_group": grain["group"], "program": grain["program"], "whatsapp_account": account,
	}).insert(ignore_permissions=True).name


def _lead(mobile_no, grain):
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "Charac Lead", "status": "New", "mobile_no": mobile_no,
		"custom_vertical": grain["vertical"], "custom_group": grain["group"],
		"custom_current_program": grain["program"],
	}).insert(ignore_permissions=True).name


class TestWATIAdapterCharacterisation(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.account = _account(_ROUTED_ACCOUNT, "919900001111")
		cls.other_account = _account(_UNROUTED_ACCOUNT, "919900002222")
		cls.routing = _routing(_GRAIN, cls.account)
		cls.lead = _lead("+" + _WA_ID, _GRAIN)
		# A second lead on the SAME account but a different number — the shared-message_id fixture. It never matches a corpus waId, so it never joins an inbound mirror.
		cls.other_lead = _lead("+919900000002", _GRAIN)

	@classmethod
	def tearDownClass(cls):
		cls._purge_messages()
		frappe.delete_doc("CRM Lead", cls.lead, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM Lead", cls.other_lead, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM WhatsApp Routing", cls.routing, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.account, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.other_account, force=True, ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def _purge_messages(cls):
		# The ingest helpers commit, so FrappeTestCase's rollback cannot undo them — clean explicitly.
		names = frappe.get_all(
			"WhatsApp Message",
			filters={"whatsapp_account": ["in", [_ROUTED_ACCOUNT, _UNROUTED_ACCOUNT]]},
			pluck="name",
		)
		names += frappe.get_all(
			"WhatsApp Message", filters={"reference_name": ["in", [cls.lead, cls.other_lead]]}, pluck="name"
		)
		for name in set(names):
			frappe.delete_doc("WhatsApp Message", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		self._purge_messages()

	# --- helpers -----------------------------------------------------------------
	def _rows(self, **filters):
		return frappe.get_all(
			"WhatsApp Message", filters=filters, fields=["*"], order_by="creation asc"
		)

	def _our_sent_row(self, local_message_id, status=None, lead=None):
		"""A row this CRM sent: `message_id` holds the WATI localMessageId — the anchor every
		status event matches on."""
		doc = frappe.get_doc({
			"doctype": "WhatsApp Message", "type": "Outgoing", "to": _WA_ID,
			"message": "our own send", "content_type": "text", "message_type": "Manual",
			"message_id": local_message_id, "status": status, "whatsapp_account": self.account,
			"reference_doctype": "CRM Lead", "reference_name": lead or self.lead,
		})
		doc.flags.tatva_ingested = True  # never re-send a fixture row through the provider
		return doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	def _stub_media(self, content=b"test-media-bytes", fail=False):
		"""No network, ever: the corpus `data` URLs are LIVE WATI URLs. `get_media` is stubbed and
		the File layer is stubbed to a fake proxy URL (real bytes are the storage suite's job)."""
		get_media = mock.Mock(side_effect=Exception("boom")) if fail else mock.Mock(
			return_value=(content, "application/octet-stream")
		)
		ensure = mock.Mock(return_value=frappe._dict(file_url="/api/method/tc.file?id=stub"))
		return mock.patch.object(transport, "get_media", get_media), mock.patch.object(
			media_module, "ensure_lead_media", ensure
		)

	# ============================================================================= screen(payload, None, account) -> (wanted, reason) =============================================================================
	def test_screen_wants_every_inbound_message_when_a_lead_holds_the_number(self):
		for case in _INBOUND_CASES:
			with self.subTest(case=case):
				self.assertEqual(adapter.screen(_payload(case), None, self.account), (True, None))

	def test_screen_declines_inbound_when_no_lead_holds_the_number(self):
		for case in _INBOUND_CASES:
			with self.subTest(case=case):
				self.assertEqual(
					adapter.screen(_payload(case, waId=_UNKNOWN_WA_ID), None, self.account),
					(False, f"no CRM lead holds the number {_UNKNOWN_WA_ID}"),
				)

	def test_screen_wants_v2_sent_events_via_the_lead_when_no_row_of_ours_matches(self):
		for case in _SENT_V2_CASES:
			with self.subTest(case=case):
				self.assertEqual(adapter.screen(_payload(case), None, self.account), (True, None))

	def test_screen_wants_v2_sent_events_via_our_row_even_with_no_lead_on_the_number(self):
		for case in _SENT_V2_CASES:
			with self.subTest(case=case):
				p = _payload(case, waId=_UNKNOWN_WA_ID)
				self._our_sent_row(p["localMessageId"])
				self.assertEqual(adapter.screen(p, None, self.account), (True, None))
				self._purge_messages()

	def test_screen_declines_v2_sent_events_with_neither_a_row_nor_a_lead(self):
		for case in _SENT_V2_CASES:
			with self.subTest(case=case):
				self.assertEqual(
					adapter.screen(_payload(case, waId=_UNKNOWN_WA_ID), None, self.account),
					(False, f"neither a message we sent nor a number any CRM lead holds ({_UNKNOWN_WA_ID})"),
				)

	def test_screen_declines_status_v2_events_for_a_message_this_crm_did_not_send(self):
		# CHANGED 2026-07-19 (recovery): the DECISION is identical — an orphan status is still declined, and the ingest path is untouched. What changed is that the reason now also says what became of the message: with the recovery switch off (its shipped state, and this suite's state) it says so, and the status is dropped exactly as it always was. The full recovery behaviour is pinned in test_recovery.py; this suite pins that turning it on changed nothing here.
		for case in _STATUS_V2_CASES:
			with self.subTest(case=case):
				self.assertEqual(
					adapter.screen(_payload(case), None, self.account),
					(False, "a status update for a message this CRM did not send; recovery is switched off"),
				)

	def test_screen_wants_status_v2_events_once_our_row_carries_the_local_message_id(self):
		for case in _STATUS_V2_CASES:
			with self.subTest(case=case):
				self._our_sent_row(CORPUS[case]["localMessageId"])
				self.assertEqual(adapter.screen(_payload(case), None, self.account), (True, None))
				self._purge_messages()

	def test_screen_declines_every_non_v2_twin_as_not_ingested(self):
		# NOTE: believed-buggy, pinned deliberately — the non-_v2 twin of a status event is the ONLY one carrying `assigneeId`/prose `text`, and it is discarded wholesale. It is declined for lacking `localMessageId`, not for being a duplicate, so a future WATI payload that gains a localMessageId on the v1 would silently start double-ingesting.
		for case in _NOT_INGESTED_CASES:
			with self.subTest(case=case):
				ev = CORPUS[case]["eventType"]
				self.assertEqual(
					adapter.screen(_payload(case), None, self.account),
					(False, f"eventType {ev} is not ingested"),
				)

	def test_screen_declines_an_empty_payload_naming_no_event_type(self):
		self.assertEqual(adapter.screen({}, None, self.account), (False, "eventType (none) is not ingested"))

	def test_screen_treats_an_owner_true_message_as_not_ingested(self):
		# `message` with owner truthy falls past the inbound branch, has no localMessageId, and lands in the catch-all — the reason names eventType "message", not the owner flag.
		p = _payload("message:text", owner=True)
		self.assertEqual(adapter.screen(p, None, self.account), (False, "eventType message is not ingested"))

	def test_screen_ignores_the_account_argument_entirely(self):
		# Pinning the seam the refactor will most want to change: screen is account-blind today.
		p = _payload("message:text")
		self.assertEqual(
			adapter.screen(p, None, self.account), adapter.screen(p, None, self.other_account)
		)
		self.assertEqual(adapter.screen(p, None, None), (True, None))

	# ============================================================================= already_processed(payload) =============================================================================
	def test_already_processed_is_false_for_every_unseen_corpus_payload(self):
		for case in CORPUS:
			with self.subTest(case=case):
				self.assertFalse(adapter.already_processed(_payload(case)))

	def test_already_processed_is_true_once_the_inbound_row_exists(self):
		adapter.handle(_payload("message:text"), None, self.account)
		self.assertTrue(adapter.already_processed(_payload("message:text")))

	def test_already_processed_sees_an_outbound_ingest(self):
		# CHANGED 2026-07-19 (defect 8): was False. An ingested outbound row stores the localMessageId in `message_id`, but already_processed looked up whatsappMessageId — so a redelivery of the same sessionMessageSent_v2 read as UNPROCESSED and only the per-lead custom_provider_message_id guard deep inside the insert stopped the duplicate. It is keyed on the provider's own id now, which is what the ingest actually stores.
		adapter.handle(_payload("sessionMessageSent_v2"), None, self.account)
		self.assertEqual(len(self._rows(reference_name=self.lead, type="Outgoing")), 1)
		self.assertTrue(adapter.already_processed(_payload("sessionMessageSent_v2"), None, self.account))

	def test_two_genuine_replies_in_one_thread_are_two_messages(self):
		"""CHANGED 2026-07-19: was `..._falls_back_to_the_composite_key_without_a_wamid`, which asserted
		the defect. The fallback keyed on conversation + sender + TEXT, so a patient answering "Yes" to
		a dose check and "Yes" again days later had the second reply read as a redelivery of the first
		and silently discarded — logged as success.

		WATI's own `id` differs per message and is present on every event, so it is the only key.
		"""
		first, second = _payload("message:text"), _payload("message:text")
		first["id"], second["id"] = "wati-reply-one", "wati-reply-two"
		for p in (first, second):
			p.pop("whatsappMessageId", None)
		second["text"] = first["text"]

		self.assertFalse(adapter.already_processed(first, None, self.account))
		adapter.handle(first, None, self.account)
		self.assertTrue(adapter.already_processed(first, None, self.account))
		self.assertFalse(
			adapter.already_processed(second, None, self.account),
			"a second reply with identical text is a NEW message, not a redelivery",
		)

	def test_already_processed_composite_key_is_scoped_to_the_account(self):
		# CHANGED 2026-07-19 (defect 8): was True. The composite fallback filtered on conversation + sender + text but NOT on whatsapp_account, so a wamid-less redelivery arriving on a SECOND account read as already processed and was silently dropped — a real message, on a real account, that never landed anywhere.
		p = _payload("message:text")
		p.pop("whatsappMessageId")
		adapter.handle(_payload("message:text"), None, self.account)
		self.assertTrue(adapter.already_processed(p, None, self.account))
		self.assertFalse(adapter.already_processed(p, None, self.other_account))

	# ============================================================================= handle(...) — inbound =============================================================================
	def test_handle_inbound_text_creates_one_incoming_row_with_the_pinned_field_set(self):
		src = _payload("message:text")
		adapter.handle(src, None, self.account)
		rows = self._rows(reference_name=self.lead)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row.name, f"{self.lead}-{src['whatsappMessageId']}")
		self.assertEqual(row.type, "Incoming")
		self.assertEqual(row.get("from"), _WA_ID)
		self.assertEqual(row.message, src["text"])
		self.assertEqual(row.content_type, "text")
		self.assertEqual(row.message_id, src["whatsappMessageId"])
		self.assertEqual(row.custom_provider_message_id, src["id"])
		self.assertEqual(row.conversation_id, src["conversationId"])
		self.assertEqual(row.profile_name, "Test User")
		self.assertEqual(row.whatsapp_account, self.account)
		self.assertEqual(row.reference_doctype, "CRM Lead")
		self.assertEqual(row.reference_name, self.lead)
		self.assertFalse(row.attach)
		# NOTE: believed-buggy, pinned deliberately — an inbound row is written with NO status at all, while an ingested outbound row gets one. The tab has nothing to render for inbound.
		self.assertFalse(row.status)

	def test_handle_inbound_image_attaches_the_media_and_keeps_the_caption_as_the_message(self):
		get_media, ensure = self._stub_media()
		with get_media, ensure:
			adapter.handle(_payload("message:image"), None, self.account)
		row = self._rows(reference_name=self.lead)[0]
		self.assertEqual(row.content_type, "image")
		self.assertEqual(row.attach, "/api/method/tc.file?id=stub")
		# The corpus image carries no caption (`text` is null) -> the bubble body is empty.
		self.assertFalse(row.message)

	def test_handle_inbound_document_keeps_the_original_filename_as_the_bubble_text(self):
		get_media, ensure = self._stub_media()
		with get_media, ensure as ensure_mock:
			adapter.handle(_payload("message:document"), None, self.account)
		row = self._rows(reference_name=self.lead)[0]
		self.assertEqual(row.content_type, "document")
		self.assertEqual(row.attach, "/api/method/tc.file?id=stub")
		# WATI puts the ORIGINAL FILENAME in `text` for a document, and the adapter both names the File with it and leaves it as the message body.
		self.assertEqual(row.message, "test-document.pdf")
		self.assertEqual(ensure_mock.call_args.args[2], "test-document.pdf")

	def test_handle_inbound_document_names_the_file_by_the_wati_internal_id_not_the_wamid(self):
		get_media, ensure = self._stub_media()
		with get_media, ensure as ensure_mock:
			adapter.handle(_payload("message:document"), None, self.account)
		self.assertEqual(ensure_mock.call_args.args[1], CORPUS["message:document"]["id"])

	def test_handle_inbound_image_whose_download_fails_becomes_a_placeholder_text_row(self):
		get_media, ensure = self._stub_media(fail=True)
		with get_media, ensure:
			adapter.handle(_payload("message:image"), None, self.account)
		row = self._rows(reference_name=self.lead)[0]
		self.assertEqual(row.content_type, "text")
		self.assertEqual(row.message, "Media unavailable")
		self.assertFalse(row.attach)

	def test_handle_inbound_document_whose_download_fails_says_so_and_names_the_file(self):
		# CHANGED 2026-07-19 (defect 10): was content_type "document" with an EMPTY attach — a bubble offering a file that is not there. The placeholder used to fire only on a BLANK body, and a document's body is its filename, so it never fired for the one case that needed it most. It keys on the failed download now, and keeps the filename so the rep can still ask for it.
		get_media, ensure = self._stub_media(fail=True)
		with get_media, ensure:
			adapter.handle(_payload("message:document"), None, self.account)
		row = self._rows(reference_name=self.lead)[0]
		self.assertEqual(row.content_type, "text")
		self.assertEqual(row.message, "Media unavailable: test-document.pdf")
		self.assertFalse(row.attach)

	def test_handle_inbound_button_tap_keeps_the_button_id_title_and_reply_context(self):
		# CHANGED 2026-07-19 (defect 2): the machine-readable identity of an interactive reply (interactiveButtonReply.id) and its replyContextId were both discarded — only the echo of the button TITLE into `text` survived, so an automation could key on nothing but prose a marketer is free to reword. The id and the title are stored now, and the reply context goes on the doctype's OWN is_reply / reply_to_message_id pair rather than a second field of ours.
		src = _payload("message:interactive")
		adapter.handle(src, None, self.account)
		row = self._rows(reference_name=self.lead)[0]
		self.assertEqual(row.content_type, "interactive")
		self.assertEqual(row.message, "Test Button")
		self.assertEqual(row.custom_button_id, "btn_test_id")
		self.assertEqual(row.custom_button_title, "Test Button")
		self.assertTrue(row.is_reply)
		self.assertEqual(row.reply_to_message_id, src["replyContextId"])

	def test_handle_inbound_is_idempotent_on_a_replayed_payload(self):
		for _ in range(3):
			adapter.handle(_payload("message:text"), None, self.account)
		self.assertEqual(len(self._rows(reference_name=self.lead)), 1)

	def test_handle_inbound_drops_when_no_lead_routes_to_the_receiving_account(self):
		adapter.handle(_payload("message:text"), None, self.other_account)
		self.assertEqual(self._rows(reference_name=self.lead), [])

	def test_handle_inbound_without_an_account_is_a_noop(self):
		adapter.handle(_payload("message:text"), None, None)
		self.assertEqual(self._rows(reference_name=self.lead), [])

	def test_handle_inbound_mirrors_onto_every_lead_sharing_the_number(self):
		# The lead dedup index is (mobile, vertical, group), so the twin must differ on vertical or group — GRAINS[2] does, and a second routing rule points it at the SAME account.
		routing = _routing(GRAINS[2], self.account)
		second = _lead("+" + _WA_ID, GRAINS[2])
		try:
			# CHANGED 2026-07-19 (defect 7): used to raise UniqueValidationError and lose the event. `pin_inbound_reference` re-checked the messaging kill-switch and returned early while it was dormant, so crm's validate clobbered BOTH rows onto first-lead-by-phone and the second insert died on the (message_id, reference_name) unique index — the lead sharing the number got nothing and the whole event was lost in the worker. The second check is gone: the ingest that stamps the pin flag has already passed that switch at the front door, so the flag IS the gate. Both leads now get the message.
			adapter.handle(_payload("message:text"), None, self.account)
			wamid = CORPUS["message:text"]["whatsappMessageId"]
			rows = self._rows(message_id=wamid)
			self.assertEqual(len(rows), 2)
			self.assertEqual({r.reference_name for r in rows}, {self.lead, second})
		finally:
			for name in frappe.get_all("WhatsApp Message", filters={"reference_name": second}, pluck="name"):
				frappe.delete_doc("WhatsApp Message", name, force=True, ignore_permissions=True)
			frappe.delete_doc("CRM Lead", second, force=True, ignore_permissions=True)
			frappe.delete_doc("CRM WhatsApp Routing", routing, force=True, ignore_permissions=True)
			frappe.db.commit()

	# ============================================================================= handle(...) — outbound ingest (a message typed in the WATI portal) =============================================================================
	def test_handle_session_sent_v2_creates_one_outgoing_manual_row(self):
		src = _payload("sessionMessageSent_v2")
		adapter.handle(src, None, self.account)
		rows = self._rows(reference_name=self.lead)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row.name, f"{self.lead}-{src['id']}")
		self.assertEqual(row.type, "Outgoing")
		self.assertEqual(row.to, _WA_ID)
		self.assertEqual(row.message, src["text"])
		self.assertEqual(row.content_type, "text")
		self.assertEqual(row.message_type, "Manual")
		self.assertEqual(row.message_id, src["localMessageId"])
		self.assertEqual(row.custom_provider_message_id, src["id"])
		self.assertEqual(row.conversation_id, src["conversationId"])
		self.assertEqual(row.profile_name, "Test Operator")
		self.assertEqual(row.whatsapp_account, self.account)
		self.assertEqual(row.reference_name, self.lead)
		# No eventType mapping for sessionMessageSent_v2 -> the status falls back to statusString.
		self.assertEqual(row.status, "sent")

	def test_handle_template_sent_v2_from_the_portal_lands_as_a_valid_content_type(self):
		# CHANGED 2026-07-19 (defect 5): used to RAISE ValidationError and lose the message. A templateMessageSent_v2 for a message this CRM did not send (typed in the portal, or sent by another API client) was copied with content_type = payload `type` = "template", which is not one of the doctype's Select options — so the insert blew up in the worker after `screen` had already said it wanted it. The provider's type vocabulary is not this field's vocabulary; anything the doctype will not accept lands as plain text, and the message survives.
		src = _payload("templateMessageSent_v2")
		adapter.handle(src, None, self.account)
		rows = self._rows(reference_name=self.lead)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].content_type, "text")
		self.assertEqual(rows[0].message, src["text"])
		self.assertEqual(rows[0].type, "Outgoing")

	def test_handle_v2_sent_event_becomes_a_status_update_when_our_row_already_holds_it(self):
		src = _payload("templateMessageSent_v2")
		ours = self._our_sent_row(src["localMessageId"], status="queued")
		adapter.handle(src, None, self.account)
		self.assertEqual(len(self._rows(reference_name=self.lead)), 1)
		self.assertEqual(frappe.db.get_value("WhatsApp Message", ours.name, "status"), "sent")

	def test_handle_outbound_is_idempotent_on_the_wati_id(self):
		for _ in range(3):
			adapter.handle(_payload("sessionMessageSent_v2"), None, self.account)
		self.assertEqual(len(self._rows(reference_name=self.lead)), 1)

	def test_handle_outbound_drops_when_no_lead_routes_to_the_receiving_account(self):
		adapter.handle(_payload("sessionMessageSent_v2"), None, self.other_account)
		self.assertEqual(self._rows(reference_name=self.lead), [])

	def test_handle_outbound_without_an_account_is_a_noop(self):
		adapter.handle(_payload("sessionMessageSent_v2"), None, None)
		self.assertEqual(self._rows(reference_name=self.lead), [])

	# ============================================================================= handle(...) — status updates =============================================================================
	def test_handle_status_v2_events_write_the_mapped_status(self):
		expected = {
			"sentMessageDELIVERED_v2": "delivered",
			"sentMessageREAD_v2": "read",
			# CHANGED 2026-07-19 (defect 1): was "read". A reply is not a read — mapping it to "read" painted a patient ANSWERING the message with the same tick as one who merely opened it, and the only outcome anybody acts on was indistinguishable from silence.
			"sentMessageREPLIED_v2": "replied",
			"templateMessageFailed": "failed",
		}
		for case, status in expected.items():
			with self.subTest(case=case):
				ours = self._our_sent_row(CORPUS[case]["localMessageId"], status="sent")
				adapter.handle(_payload(case), None, self.account)
				self.assertEqual(frappe.db.get_value("WhatsApp Message", ours.name, "status"), status)
				self._purge_messages()

	def test_handle_template_failed_records_the_failure_code_and_detail(self):
		# CHANGED 2026-07-19 (defect 9): was blank. `custom_failed_reason` existed on the doctype and the payload carried failedCode 131026 / "Message undeliverable", but neither was written — an operator saw the word "failed" and had nowhere to go with it.
		case = "templateMessageFailed"
		ours = self._our_sent_row(CORPUS[case]["localMessageId"], status="sent")
		adapter.handle(_payload(case), None, self.account)
		reason = frappe.db.get_value("WhatsApp Message", ours.name, "custom_failed_reason")
		self.assertIn("131026", reason)
		self.assertIn("Message undeliverable", reason)

	def test_handle_status_updates_every_row_sharing_the_local_message_id(self):
		case = "sentMessageDELIVERED_v2"
		lmid = CORPUS[case]["localMessageId"]
		first = self._our_sent_row(lmid, status="sent")
		second = self._our_sent_row(lmid, status="sent", lead=self.other_lead)
		adapter.handle(_payload(case), None, self.account)
		for name in (first.name, second.name):
			self.assertEqual(frappe.db.get_value("WhatsApp Message", name, "status"), "delivered")

	def test_handle_status_for_an_unknown_local_message_id_is_a_noop(self):
		adapter.handle(_payload("sentMessageREAD_v2"), None, self.account)
		self.assertEqual(self._rows(reference_name=self.lead), [])

	def test_handle_scopes_a_status_update_to_the_receiving_account(self):
		# CHANGED 2026-07-19 (defect 6): the row used to tick. The status write never checked the receiving account, so a status delivered on account B ticked a row sent on account A — one programme's delivery receipt painted onto another programme's message.
		case = "sentMessageREAD_v2"
		ours = self._our_sent_row(CORPUS[case]["localMessageId"], status="sent")
		adapter.handle(_payload(case), None, self.other_account)
		self.assertEqual(frappe.db.get_value("WhatsApp Message", ours.name, "status"), "sent")
		adapter.handle(_payload(case), None, self.account)
		self.assertEqual(frappe.db.get_value("WhatsApp Message", ours.name, "status"), "read")

	def test_handle_is_a_noop_for_every_non_ingested_event_type(self):
		for case in _NOT_INGESTED_CASES:
			with self.subTest(case=case):
				adapter.handle(_payload(case), None, self.account)
				self.assertEqual(self._rows(reference_name=self.lead), [])

	def test_status_by_event_map_is_exactly_five_entries(self):
		self.assertEqual(
			adapter.STATUS_BY_EVENT,
			{
				"templateMessageSent_v2": "sent",
				"sentMessageDELIVERED_v2": "delivered",
				"sentMessageREAD_v2": "read",
				"sentMessageREPLIED_v2": "replied",  # CHANGED 2026-07-19 (defect 1): was "read"
				"templateMessageFailed": "failed",
			},
		)
		self.assertEqual(adapter.OUTBOUND_SENT_EVENTS, {"sessionMessageSent_v2", "templateMessageSent_v2"})

	# ============================================================================= account_for_payload(payload) — the replay hook =============================================================================
	def test_account_for_payload_resolves_from_a_local_message_id(self):
		case = "sentMessageREAD_v2"
		self._our_sent_row(CORPUS[case]["localMessageId"])
		self.assertEqual(adapter.account_for_payload(_payload(case)), self.account)

	def test_account_for_payload_resolves_from_a_whatsapp_message_id(self):
		adapter.handle(_payload("message:text"), None, self.account)
		self.assertEqual(adapter.account_for_payload(_payload("message:text")), self.account)

	def test_account_for_payload_fails_closed_with_no_anchor(self):
		for case in CORPUS:
			with self.subTest(case=case):
				self.assertIsNone(adapter.account_for_payload(_payload(case)))


if __name__ == "__main__":
	unittest.main()
