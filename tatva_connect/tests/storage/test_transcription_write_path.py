# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE WRITE PATH — a transcription service posts text in, and it lands through the SAME door.

THE POINT OF THIS SUITE IS THAT NOTHING NEW EXISTS. A service that transcribes our recordings and posts
the text back is not a new kind of integration:

  * its authentication is the shared spine's — per-account token, optional HMAC, optional IP allowlist,
    a raw Integration Request log and a replay button. Not one line of auth is written for it.
  * its storage is `call_media.store_transcript` — the very same call the voice adapter's parser makes.
    If a transcript could be STORED two ways, the day the two disagreed nobody would know which text was
    the call.

So the tests below mostly assert absences: no second auth, no second write, no second shape.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.storage.test_transcription_write_path
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.storage import call_media, transcription_channel
from tatva_connect.storage.adapters import transcription
from tatva_connect.webhooks import ingress, registry, spine

_ACCOUNT = "transcription-write-path-account"
_TOKEN = "transcription-write-path-token"


def _account(name=_ACCOUNT, enabled=1, token=_TOKEN):
	if frappe.db.exists(transcription.ACCOUNT_DOCTYPE, name):
		frappe.delete_doc(transcription.ACCOUNT_DOCTYPE, name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": transcription.ACCOUNT_DOCTYPE, "account_name": name, "provider": "tatva",
		"enabled": enabled, "webhook_token": token,
	}).insert(ignore_permissions=True)


def _call():
	return frappe.get_doc({
		"doctype": "CRM Call Log", "id": f"wp-{frappe.generate_hash(length=10)}", "type": "Outgoing",
		"status": "Completed", "telephony_medium": "AI Voice", "to": "+919999999999",
		"from": "+918035303509", "duration": 12,
	}).insert(ignore_permissions=True).name


def _post(call, text="The patient confirmed the appointment.", **over):
	payload = {"call": call, "text": text, "source": "whisper-v3",
	           "segments": [{"speaker": "agent", "text": text}], "summary": "Confirmed."}
	payload.update(over)
	return payload


class TestTheServiceIsJustAnotherAccountRow(FrappeTestCase):
	"""No new auth is invented. Everything below is the spine's, inherited by being a registry entry."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.account = _account()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc(transcription.ACCOUNT_DOCTYPE, _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_the_channel_declares_a_whole_ingress(self):
		cfg = registry.by_channel("transcription")
		self.assertEqual(cfg["account_doctype"], transcription.ACCOUNT_DOCTYPE)
		self.assertEqual(cfg["token_field"], "webhook_token")
		self.assertTrue(callable(cfg["targets"]), "a channel with no targets can publish no URL to register")

	def test_the_url_carries_the_token_and_no_vendor(self):
		url = registry.by_channel("transcription")["targets"]("https://crm.example", self.account, _TOKEN)[0]["url"]
		self.assertEqual(url, f"https://crm.example/webhooks/transcription/{_TOKEN}")
		self.assertNotIn("tatva", url.rsplit("/", 1)[0].replace("crm.example", ""),
		                 "the service is named by the account row, never by the URL")

	def test_the_digest_is_derived_on_save(self):
		"""A Password column cannot be indexed, so authentication is one read against this digest."""
		self.assertEqual(
			frappe.db.get_value(transcription.ACCOUNT_DOCTYPE, _ACCOUNT, "webhook_token_hash"),
			ingress.token_digest(_TOKEN),
		)

	def test_the_right_token_resolves_the_account_and_a_wrong_one_resolves_nothing(self):
		cfg = registry.by_channel("transcription")
		self.assertEqual(ingress._account_for_token(cfg, _TOKEN), _ACCOUNT)
		self.assertIsNone(ingress._account_for_token(cfg, "not-the-token"))
		self.assertIsNone(ingress._account_for_token(cfg, ""))

	def test_a_disabled_account_authenticates_nothing(self):
		"""Turning an integration off must STOP its traffic, not merely hide it from a list."""
		frappe.db.set_value(transcription.ACCOUNT_DOCTYPE, _ACCOUNT, "enabled", 0)
		frappe.db.commit()
		try:
			self.assertIsNone(ingress._account_for_token(registry.by_channel("transcription"), _TOKEN))
		finally:
			frappe.db.set_value(transcription.ACCOUNT_DOCTYPE, _ACCOUNT, "enabled", 1)
			frappe.db.commit()

	def test_the_channel_is_dormant_by_default(self):
		"""Off is not a bug. A posted transcript is raw-logged and replayable rather than acted on."""
		self.assertFalse(transcription_channel.is_enabled())

	def test_the_endpoint_is_the_spine_and_nothing_else(self):
		import inspect

		source = inspect.getsource(transcription_channel.webhook)
		self.assertIn("spine.receive", source)
		self.assertNotIn("token", source.split("def webhook")[1],
		                 "the endpoint must not read a token — the spine does that")


class TestNothingIsGuessedAboutWhichCallThisIs(FrappeTestCase):
	"""A clinical note filed against the wrong patient's call is worse than one that never arrived, so
	every refusal here is deliberate and every one of them is RECORDED and replayable."""

	def setUp(self):
		self.call = _call()

	def test_a_transcript_with_no_call_is_declined(self):
		wanted, reason = transcription.screen(_post(None), None, _ACCOUNT)
		self.assertFalse(wanted)
		self.assertIn("no call", reason)

	def test_a_transcript_for_a_call_we_do_not_hold_is_declined(self):
		wanted, reason = transcription.screen(_post("no-such-call"), None, _ACCOUNT)
		self.assertFalse(wanted)
		self.assertIn("not a call this CRM holds", reason)

	def test_a_transcript_with_nothing_in_it_is_declined(self):
		wanted, reason = transcription.screen(_post(self.call, text="", summary=None), None, _ACCOUNT)
		self.assertFalse(wanted)
		self.assertIn("neither text nor a summary", reason)

	def test_a_malformed_segment_list_is_declined(self):
		wanted, reason = transcription.screen(_post(self.call, segments={"not": "a list"}), None, _ACCOUNT)
		self.assertFalse(wanted)
		self.assertIn("segments", reason)

	def test_a_well_formed_transcript_is_wanted(self):
		self.assertEqual(transcription.screen(_post(self.call), None, _ACCOUNT), (True, None))

	def test_a_summary_alone_is_enough(self):
		"""Sparseness is the contract: a service that returns only a summary fills fewer boxes."""
		self.assertEqual(
			transcription.screen(_post(self.call, text=None), None, _ACCOUNT), (True, None)
		)


class TestItStoresThroughTheOneDoor(FrappeTestCase):
	"""Not a second write path — the same function the voice parser calls."""

	def setUp(self):
		self.call = _call()

	def test_handle_calls_the_shared_door_and_nothing_else(self):
		with patch.object(call_media, "store_transcript", wraps=call_media.store_transcript) as door:
			transcription.handle(_post(self.call), None, _ACCOUNT)
		door.assert_called_once()
		self.assertEqual(door.call_args.args[0], self.call)

	def test_the_text_really_lands_in_the_canonical_shape(self):
		transcription.handle(_post(self.call), None, _ACCOUNT)
		row = frappe.db.get_value(call_media.MEDIA_DT, self.call, "*", as_dict=True)
		self.assertEqual(row.text, "The patient confirmed the appointment.")
		self.assertEqual(row.transcript_source, "whisper-v3")
		self.assertEqual(len(frappe.parse_json(row.segments)), 1)

	def test_a_service_transcript_replaces_a_providers(self):
		"""THE PRODUCT DECISION. A re-transcription is a correction, not history — and `raw` and `source`
		move with the text they explain, or the row would document a text it no longer holds."""
		call_media.store_transcript(self.call, {"source": "bolna", "text": "garbled", "raw": '{"a":1}'})
		transcription.handle(_post(self.call), None, _ACCOUNT)
		row = frappe.db.get_value(call_media.MEDIA_DT, self.call, "*", as_dict=True)
		self.assertEqual(row.text, "The patient confirmed the appointment.")
		self.assertEqual(row.transcript_source, "whisper-v3")
		self.assertIn("whisper-v3", row.raw)
		self.assertEqual(frappe.db.count(call_media.MEDIA_DT, {"call": self.call}), 1)

	def test_a_redelivery_is_short_circuited_before_the_worker_runs(self):
		transcription.handle(_post(self.call), None, _ACCOUNT)
		self.assertTrue(transcription.already_processed(_post(self.call), None, _ACCOUNT))

	def test_a_corrected_transcript_is_not_mistaken_for_a_redelivery(self):
		transcription.handle(_post(self.call), None, _ACCOUNT)
		self.assertFalse(
			transcription.already_processed(_post(self.call, text="Actually declined."), None, _ACCOUNT)
		)

	def test_a_recording_is_never_touched_by_a_transcript_post(self):
		"""One door per ARTIFACT. Text arriving must not disturb what we do or do not hold as audio."""
		with patch.object(call_media, "store_recording") as audio:
			transcription.handle(_post(self.call), None, _ACCOUNT)
		audio.assert_not_called()
		# Blank, the shipped default: no producer has said anything about audio for this call.
		self.assertFalse(frappe.db.get_value(call_media.MEDIA_DT, self.call, "recording_state"))


class TestTheAdapterResolvesThroughTheOneRegistry(FrappeTestCase):
	"""No second adapter registry and no second resolution path — `channels.resolve` answers for this
	channel exactly as it does for WhatsApp, voice and telephony."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_account()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc(transcription.ACCOUNT_DOCTYPE, _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def test_the_account_row_names_the_adapter(self):
		from tatva_connect.channels import resolve

		self.assertIs(resolve.adapter_for(_ACCOUNT, transcription.ACCOUNT_DOCTYPE), transcription)

	def test_the_spine_finds_the_same_adapter(self):
		self.assertIs(spine._adapter_for("transcription", _ACCOUNT), transcription)

	def test_a_replayed_delivery_re_derives_its_account(self):
		"""A stored row carries no token, so the payload's own adapter identifies it."""
		self.assertEqual(transcription.account_for_payload({"call": "anything"}, None), _ACCOUNT)

	def test_a_payload_that_is_not_ours_is_declined(self):
		self.assertIsNone(transcription.account_for_payload({"id": "a-voice-callback"}, None))
