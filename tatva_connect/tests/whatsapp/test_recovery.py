# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Orphan-status RECOVERY, and the v3 read path underneath it.

A status event names a message by `conversationId` + `id` — the two identities present on 100% of live
status traffic, where a phone number is present on none of it. When no local row carries the status's
correlation id the message is not a phantom: it exists on the provider, and this suite is the proof
that we now go and get it instead of dropping the receipt.

What is pinned here, one test per rule:

  * an orphan status QUEUES a recovery, and the recovery makes the message appear with its status on it
  * running it twice leaves ONE row — a message already held is never fetched or inserted again
  * a recovered MEDIA message gets its bytes filed through the storage layer, stamped with the
    provider's message id, not re-implemented here
  * a recovered message fires NO entry trigger and rings NO phone — it is backfill, not a live event
  * a recovery that fails leaves the ingest path exactly as it was
  * `normalize_history` and the webhook `normalize` produce ONE envelope for one logical message

NO NETWORK. Every provider read is stubbed at `transport`, the module that owns the wire — so the
tests exercise the real adapter, the real ingest and the real storage seam, and never a WATI URL.
"""
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.channels import event as channel_event
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.whatsapp import channel, ingest, recovery, transport
from tatva_connect.whatsapp import media as media_module
from tatva_connect.whatsapp import wati as adapter
from tatva_connect.workflow_engine import ENGINE_SWITCH

_GRAIN = GRAINS[0]
_WA_ID = "919900000051"
_ACCOUNT = "Recovery-test-account"
_CONVERSATION = "conv-recovery-0001"
_PROVIDER_ID = "wati-msg-0001"
_LOCAL_ID = "local-msg-0001"
_ANCHOR_ID = "wati-msg-anchor"


def _status_payload(**overrides):
	"""A real WATI status event shape: it names a conversation and a message id, and NO phone number."""
	payload = {
		"eventType": "sentMessageDELIVERED_v2",
		"id": _PROVIDER_ID,
		"localMessageId": _LOCAL_ID,
		"conversationId": _CONVERSATION,
		"statusString": "DELIVERED",
	}
	payload.update(overrides)
	return payload


# The REAL v3 item shape: the exact field union measured over 100 live items, verbatim and complete. Nothing may be added to this without measuring it first — a stub carrying a field the API does not send is worse than no test, because it proves a contract that does not exist. Note what is NOT here: no contact identifier of any kind, no local_message_id, no `data` media URL.
_V3_FIELDS = (
	"actor", "assigned_id", "assignee", "avatar_url", "bot_type", "conversation_id", "created",
	"detailed_event_description", "event_description", "event_type", "failed_detail", "final_text",
	"id", "operator_name", "owner", "status", "status_string", "text", "ticket_id", "timestamp",
	"topic_name", "type",
)


def _v3_item(**overrides):
	"""A v3 history item exactly as the live endpoint returns one — every measured field, no others."""
	item = dict.fromkeys(_V3_FIELDS)
	item.update({
		"id": _PROVIDER_ID,
		"conversation_id": _CONVERSATION,
		"event_type": "message",
		"final_text": "recovered from the conversation",
		"text": "recovered from the conversation",
		"type": "text",
		"owner": True,
		"operator_name": "agent@example.com",
		"status": 3,
		"status_string": "SENT",
		"created": "2026-07-19T09:00:00Z",
		"timestamp": "1784800000",
	})
	item.update(overrides)
	unknown = set(item) - set(_V3_FIELDS)
	if unknown:
		raise AssertionError(f"the live v3 endpoint sends no such field(s): {sorted(unknown)}")
	return item


def _account_row():
	if frappe.db.exists("WhatsApp Account", _ACCOUNT):
		frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": _ACCOUNT, "status": "Active",
		"url": "https://live-mt-server.wati.io/360078", "token": "recovery-test-token",
		"custom_provider": "WATI", "custom_wati_channel_number": "919900003333",
	}).insert(ignore_permissions=True).name


def _routing_row(account):
	key = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}"
	if frappe.db.exists("CRM WhatsApp Routing", key):
		frappe.delete_doc("CRM WhatsApp Routing", key, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "CRM WhatsApp Routing", "vertical": _GRAIN["vertical"],
		"psp_group": _GRAIN["group"], "program": _GRAIN["program"], "whatsapp_account": account,
	}).insert(ignore_permissions=True).name


def _lead_row():
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "Recovery Lead", "status": "New", "mobile_no": "+" + _WA_ID,
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"],
	}).insert(ignore_permissions=True).name


def _thread_anchor(lead):
	"""One message we ALREADY hold on the conversation — the precondition attribution really runs on.

	A v3 item names no contact, so this row is what tells recovery whose thread it is. It is also exactly
	what the real orphan case looks like: a webhook blip inside a thread we otherwise hold in full.
	"""
	doc = frappe.get_doc({
		"doctype": "WhatsApp Message", "type": "Incoming", "from": _WA_ID,
		"message": "an earlier message in this thread", "content_type": "text",
		"custom_provider_message_id": _ANCHOR_ID, "conversation_id": _CONVERSATION,
		"whatsapp_account": _ACCOUNT, "reference_doctype": "CRM Lead", "reference_name": lead,
	})
	doc.flags.tatva_ingested = True  # never re-send a fixture row through the provider
	doc.flags.tatva_pinned_lead = lead
	return doc.insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no user input


def _switch(key, enabled):
	"""Flip an operator switch, returning what it was. The caller restores it — see tearDown."""
	was = frappe.db.get_value("CRM Tatva Automation", key, "enabled")
	frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1 if enabled else 0)
	frappe.db.commit()
	return was


class TestWhatsAppRecovery(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.account = _account_row()
		cls.routing = _routing_row(cls.account)
		cls.lead = _lead_row()

	@classmethod
	def tearDownClass(cls):
		cls._purge_messages()
		frappe.delete_doc("CRM Lead", cls.lead, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM WhatsApp Routing", cls.routing, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.account, force=True, ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def _purge_messages(cls):
		# The ingest helpers commit, so FrappeTestCase's rollback cannot undo them — clean explicitly.
		for name in frappe.get_all("WhatsApp Message", filters={"whatsapp_account": _ACCOUNT}, pluck="name"):
			frappe.delete_doc("WhatsApp Message", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		# Captured, not assumed. Forcing these OFF in tearDown silently disarmed an operator's own configuration on any bench where recovery or the engine was legitimately on.
		self._switches = {
			channel.SWITCH_RECOVERY: _switch(channel.SWITCH_RECOVERY, True),
			ENGINE_SWITCH: frappe.db.get_value("CRM Tatva Automation", ENGINE_SWITCH, "enabled"),
		}
		self.anchor = _thread_anchor(self.lead)

	def tearDown(self):
		for key, was in self._switches.items():
			_switch(key, was)
		self._purge_messages()

	# --- helpers -----------------------------------------------------------------
	def _rows(self, **filters):
		"""Every row this account holds EXCEPT the thread anchor — i.e. what recovery actually wrote."""
		filters.setdefault("whatsapp_account", _ACCOUNT)
		filters["name"] = ["!=", self.anchor]
		return frappe.get_all("WhatsApp Message", filters=filters, fields=["*"], order_by="creation asc")

	def _stub_conversation(self, *items, media=None):
		"""Stub the ONE wire call the v3 read makes, plus the media read. No URL is ever built."""
		def one_page(account, target, page_number=1, page_size=100):
			"""Page 1 holds the conversation; page 2 is empty, which is what ends the walk."""
			return list(items) if page_number == 1 else []

		pages = mock.Mock(side_effect=one_page)
		return (
			mock.patch.object(transport, "fetch_conversation_messages", pages),
			mock.patch.object(transport, "fetch_message_media", mock.Mock(return_value=media)),
		)

	# ============================================================================= The core: an orphan status recovers the message it names. =============================================================================
	def test_an_orphan_status_queues_a_recovery_instead_of_being_dropped(self):
		"""CHANGED 2026-07-19: the queue moved out of `screen` into `handle`.

		Screening decides whether a delivery is wanted; it must not ACT. With the enqueue inside it, a
		DLQ replay re-screened the same declined status and fired a fresh provider fetch every time.
		An orphan status is now WANTED, and the worker recovers the message it names.
		"""
		wanted, _reason = adapter.screen(_status_payload(), None, self.account)
		self.assertTrue(wanted, "an orphan status is wanted — the worker decides what to do with it")

		with mock.patch.object(frappe, "enqueue") as enqueued:
			adapter.handle(_status_payload(), None, self.account)
		self.assertEqual(enqueued.call_args.args[0], "tatva_connect.whatsapp.recovery.recover")
		self.assertEqual(enqueued.call_args.kwargs["account"], self.account)
		self.assertEqual(enqueued.call_args.kwargs["payload"]["id"], _PROVIDER_ID)

	def test_screening_an_orphan_status_twice_fetches_nothing(self):
		"""The regression the move exists to prevent: re-screening is free."""
		with mock.patch.object(frappe, "enqueue") as enqueued:
			for _ in range(3):
				adapter.screen(_status_payload(), None, self.account)
		enqueued.assert_not_called()

	def test_recovery_inserts_the_one_message_and_applies_the_status_to_it(self):
		pages, media = self._stub_conversation(_v3_item())
		with pages, media:
			recovery.recover(self.account, _status_payload())
		rows = self._rows()
		self.assertEqual(len(rows), 1, "recovery must insert exactly the ONE message the status named")
		self.assertEqual(rows[0].custom_provider_message_id, _PROVIDER_ID)
		self.assertEqual(rows[0].message, "recovered from the conversation")
		self.assertEqual(rows[0].reference_name, self.lead)
		self.assertEqual(rows[0].status, "delivered", "the status that triggered recovery must land on it")

	def test_recovery_inserts_only_the_named_message_never_the_conversation(self):
		"""Surgical. A status event is not authority to rewrite a thread — the other three messages in
		this conversation are real, and none of them is what this receipt was about."""
		others = [_v3_item(id=f"other-{n}") for n in range(3)]
		pages, media = self._stub_conversation(*others, _v3_item())
		with pages, media:
			recovery.recover(self.account, _status_payload())
		self.assertEqual([r.custom_provider_message_id for r in self._rows()], [_PROVIDER_ID])

	def test_a_v3_item_carrying_no_contact_field_still_attributes_via_the_conversation(self):
		"""THE production path. A v3 item has no contact identifier of any kind, so attribution cannot
		come from the provider — it comes from the conversation's own rows, which we already hold."""
		item = _v3_item()
		for absent in ("wa_id", "waId", "phone", "phone_number", "contact_id", "bsuid", "number", "from"):
			self.assertNotIn(absent, item, "the live v3 endpoint sends no contact identifier")
		pages, media = self._stub_conversation(item)
		with pages, media:
			recovery.recover(self.account, _status_payload())
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].reference_name, self.lead, "attributed from the conversation we hold")
		self.assertEqual(rows[0].to, _WA_ID, "the subject number came from the thread, not the item")
		self.assertEqual(
			rows[0].message_id, _LOCAL_ID,
			"a v3 item has no local_message_id — it must be carried down from the status, or the row "
			"can never be ticked by the receipt that recovered it",
		)

	def test_a_conversation_we_hold_nothing_on_is_dropped_and_said_so(self):
		"""The accepted limit, named. With no row on the conversation there is nothing to attribute
		from, and filing a message against the wrong patient is worse than not filing it. It must also
		never reach the provider — there would be no point in what came back."""
		pages, media = self._stub_conversation(_v3_item(conversation_id="conv-we-never-saw"))
		with pages as reads, media:
			recovery.recover(self.account, _status_payload(conversationId="conv-we-never-saw"))
		self.assertEqual(self._rows(), [])
		reads.assert_not_called()

	def test_recovering_twice_leaves_one_row_and_asks_the_provider_once(self):
		"""Idempotent: the provider re-sends a status, and the second pass must neither insert a second
		row nor go back to the provider for a message it already holds."""
		pages, media = self._stub_conversation(_v3_item())
		with pages as reads, media:
			recovery.recover(self.account, _status_payload())
			reads_after_first = reads.call_count
			recovery.recover(self.account, _status_payload())
		self.assertEqual(len(self._rows()), 1, "recovering twice must leave exactly one row")
		self.assertEqual(
			reads.call_count, reads_after_first,
			"a message we already hold must not send us back to the provider",
		)

	# ============================================================================= Media — filed through the storage layer, never re-implemented here. =============================================================================
	def test_a_recovered_media_message_is_filed_through_the_storage_layer(self):
		"""The bytes come from the v3 media endpoint, which also names the real filename; everything
		about privacy, Azure and the URL belongs to storage.file_manager and is not decided here."""
		item = _v3_item(type="document", text="discharge summary")
		pages, media = self._stub_conversation(item, media=(b"%PDF-1.4 bytes", "application/pdf", "report.pdf"))
		stored = mock.Mock(return_value=frappe._dict(file_url="/api/method/tc.file?id=recovered"))
		with pages, media, mock.patch.object(media_module, "ensure_lead_media", stored):
			recovery.recover(self.account, _status_payload())
		stored.assert_called_once()
		lead, message_uid, filename, content = stored.call_args.args
		self.assertEqual(lead, self.lead)
		self.assertEqual(message_uid, _PROVIDER_ID, "the provider's message id is the idempotency stamp")
		self.assertEqual(filename, "report.pdf", "the provider named the file; do not guess one")
		self.assertEqual(content, b"%PDF-1.4 bytes")
		row = self._rows()[0]
		self.assertEqual(row.content_type, "document")
		self.assertEqual(row.attach, "/api/method/tc.file?id=recovered")

	# ============================================================================= A recovered message is BACKFILL — it starts nothing. =============================================================================
	def test_a_recovered_message_fires_no_entry_trigger(self):
		"""A message pulled out of the past must not open a journey. With the engine armed, a live insert
		reaches entry detection and a recovered one must not."""
		_switch(ENGINE_SWITCH, True)
		from tatva_connect.workflow_engine import triggers

		pages, media = self._stub_conversation(_v3_item(owner=False, id="inbound-1"))
		with pages, media, mock.patch.object(triggers, "_trigger_context", return_value=None) as detected:
			recovery.recover(self.account, _status_payload(id="inbound-1"))
			self.assertEqual(
				[c for c in detected.call_args_list if c.args[0].doctype == "WhatsApp Message"], [],
				"a recovered message reached entry detection — it is backfill, not a live event",
			)
		# The same insert made LIVE does reach it, so the assertion above is not vacuous.
		with mock.patch.object(triggers, "_trigger_context", return_value=None) as detected:
			ingest.apply(
				adapter.normalize_history(
					_v3_item(owner=False, id="live-1"), account=self.account, number=_WA_ID
				)
			)
			self.assertTrue(
				[c for c in detected.call_args_list if c.args[0].doctype == "WhatsApp Message"],
				"a live message must still reach entry detection, or this suite proves nothing",
			)

	def test_a_recovered_message_rings_nobodys_phone(self):
		"""Filing a reply from last week must not notify a rep that a patient just replied."""
		from tatva_connect.notifications import dispatch

		pages, media = self._stub_conversation(_v3_item(owner=False, id="inbound-2"))
		with pages, media, mock.patch.object(dispatch, "notify") as notified:
			recovery.recover(self.account, _status_payload(id="inbound-2"))
		self.assertEqual(
			[c for c in notified.call_args_list if c.args and c.args[0] == "WhatsApp::Message::received"], []
		)

	# ============================================================================= Fail soft — recovery can never break ingest. =============================================================================
	def test_a_failed_recovery_writes_nothing_and_never_raises(self):
		with mock.patch.object(
			transport, "fetch_conversation_messages", side_effect=Exception("WATI is down")
		):
			recovery.recover(self.account, _status_payload())  # must not raise
		self.assertEqual(self._rows(), [])

	def test_a_failed_recovery_leaves_the_ingest_path_intact(self):
		"""A recovery that cannot be queued must not take the delivery down with it."""
		with mock.patch.object(frappe, "enqueue", side_effect=Exception("redis is down")):
			adapter.handle(_status_payload(), None, self.account)
		self.assertEqual(self._rows(), [], "a failed queue writes nothing")

	def test_recovery_is_dormant_until_the_operator_turns_it_on(self):
		# CHANGED 2026-07-19: drives `handle`, not `screen`. The decision moved — screening no longer acts, so the switch is read where the work would be done.
		_switch(channel.SWITCH_RECOVERY, False)
		payload = _status_payload()
		with mock.patch.object(frappe, "enqueue") as enqueued:
			adapter.handle(payload, None, self.account)
		enqueued.assert_not_called()
		self.assertIn("switched off", recovery.queue(adapter.normalize(payload, self.account)))

	def test_a_status_that_names_no_conversation_is_not_recoverable(self):
		"""Recovery keys on the conversation and the message id, and on nothing else. A status without
		them is dropped exactly as it always was."""
		payload = _status_payload(conversationId=None)
		with mock.patch.object(frappe, "enqueue") as enqueued:
			adapter.handle(payload, None, self.account)
		enqueued.assert_not_called()
		self.assertIn("names no conversation", recovery.queue(adapter.normalize(payload, self.account)))

	# ============================================================================= One envelope, two producers. =============================================================================
	def test_both_normalizers_build_the_same_envelope_for_the_same_message(self):
		"""The webhook dialect is camelCase and the v3 dialect is snake_case. That difference must die at
		the adapter: one logical message, one envelope, whichever door it came through."""
		webhook = adapter.normalize(
			{
				"eventType": "message", "owner": False, "id": _PROVIDER_ID, "waId": _WA_ID,
				"conversationId": _CONVERSATION, "text": "hello", "type": "text",
			},
			self.account,
		)
		history = adapter.normalize_history(
			_v3_item(owner=False, final_text="hello", text="hello", type="text"),
			self.account, number=_WA_ID,
		)
		self.assertEqual(set(webhook.keys()), set(history.keys()))
		self.assertEqual(set(webhook.keys()), set(channel_event._FIELDS))
		for field in ("kind", "channel", "provider", "account", "provider_message_id",
		              "conversation_id", "subject_number", "text", "media_type"):
			self.assertEqual(webhook.get(field), history.get(field), field)

	def test_history_reads_direction_from_owner_not_from_the_items_event_type(self):
		"""Every v3 item says event_type "message" whichever way it went. Believing that would file an
		agent's outbound message as a status and lose it."""
		outbound = adapter.normalize_history(
			_v3_item(owner=True), self.account, number=_WA_ID, correlation_id=_LOCAL_ID
		)
		inbound = adapter.normalize_history(_v3_item(owner=False), self.account, number=_WA_ID)
		self.assertEqual(outbound.kind, "outbound_echo")
		self.assertEqual(outbound.correlation_id, _LOCAL_ID)
		self.assertEqual(inbound.kind, "inbound")

	# ============================================================================= The two base URLs. =============================================================================
	def test_v1_is_tenant_scoped_and_v3_is_host_root(self):
		"""Measured: the tenant path on v3 returns 404. One function knows the difference."""
		account = frappe.get_doc("WhatsApp Account", self.account)
		self.assertEqual(transport.base_url(account, transport.API_V1), "https://live-mt-server.wati.io/360078")
		self.assertEqual(transport.base_url(account, transport.API_V3), "https://live-mt-server.wati.io")

	def test_a_trailing_slash_cannot_break_either_base(self):
		account = frappe._dict(url="https://live-mt-server.wati.io/360078/")
		self.assertEqual(transport.base_url(account, transport.API_V1), "https://live-mt-server.wati.io/360078")
		self.assertEqual(transport.base_url(account, transport.API_V3), "https://live-mt-server.wati.io")

	def test_an_unknown_api_version_is_refused(self):
		account = frappe.get_doc("WhatsApp Account", self.account)
		with self.assertRaises(ValueError):
			transport.base_url(account, "v2")

	# ============================================================================= The declaration is the contract. =============================================================================
	def test_wati_declares_both_recovery_capabilities_and_implements_them(self):
		"""A capability nobody implements is a lie the consumer believes."""
		for capability, method in (
			("recover_message", "recover_message"),
			("recover_media", "fetch_media_by_message_id"),
			("backfill", "normalize_history"),
		):
			self.assertTrue(adapter.DECLARATION.can(capability), capability)
			self.assertTrue(callable(getattr(adapter, method, None)), method)

	def test_a_provider_that_cannot_recover_simply_drops_the_status(self):
		"""The whole point of declaring it: no vendor is required to grow this, and one that has not
		keeps today's behaviour with no code path of its own."""
		mute = adapter.DECLARATION.__class__(
			channel="whatsapp", provider="Mute", account_doctype="WhatsApp Account",
			outcomes=frozenset({"delivered"}), capabilities=frozenset(),
		)
		payload = _status_payload()
		with mock.patch.object(adapter, "DECLARATION", mute), mock.patch.object(frappe, "enqueue") as enqueued:
			adapter.handle(payload, None, self.account)
			reason = recovery.queue(adapter.normalize(payload, self.account))
		self.assertIn("cannot recover", reason)
		enqueued.assert_not_called()
