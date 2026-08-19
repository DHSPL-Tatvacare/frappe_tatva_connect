# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A telephony recording becomes OURS — the webhook, the storage layer, the blob, and the play button.

THE DEFECT THIS PINS. A CDR's `recording_url` was copied onto the Call Log as text and the audio was
never fetched, so the player reached the provider's host at the moment a rep pressed play — and answered
404 the day the provider aged the file out. Nothing was ever in Azure to fall back to.

NOTHING NEW IS BUILT HERE AND NOTHING IS FAKED EXCEPT THE PROVIDER'S HTTP. The adapter answers the same
`RecordingRef` the AI-voice adapter answers, the writer hands it to the same `storage.call_media` door,
and the bytes land through the same `file_manager` -> File doc_events -> Azure path as every other file
in the app. These tests upload to the REAL container and assert the blob is really there, reusing
`FileLayerCase` for that and `fixtures.config` for the operator rows a captured call needs.

`writer.write` COMMITS, so `FrappeTestCase`'s rollback cannot undo these rows: every call is deleted in
tearDown, which takes its media row and its File with it, and the offload switch is handed back exactly.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.telephony.test_recording_handoff
"""
from unittest.mock import patch

import frappe

from tatva_connect.channels import contract
from tatva_connect.storage import call_media
from tatva_connect.storage.blob_store import blob_key_from_url
from tatva_connect.telephony import reconcile, writer
from tatva_connect.telephony.adapters import acefone
from tatva_connect.tests.storage.test_call_media import _AUDIO, _FakeResponse
from tatva_connect.tests.storage.test_file_layer_registry import FileLayerCase
from tatva_connect.tests.telephony.fixtures import config

DID = "9000007181"
CUSTOMER = "9000012399"
_OFFLOAD = "Storage::Azure::offload"
# A reserved host: the fetch is mocked, and nothing here can reach the network even if it were not.
_PROVIDER_URL = "https://recordings.acefone.invalid/file/recording?callId=42&type=rec&token=abc"


def _cdr(call_id, *, recording_url=_PROVIDER_URL, call_status="answered"):
	"""One Acefone hangup CDR, in the shape the live webhook sends — the corpus dialect, Dialer inbound."""
	return {
		"call_id": call_id,
		"uuid": "6a5384e2b1c07",
		"direction": "Dialer (inbound)",
		"call_status": call_status,
		"call_connected": "1" if call_status == "answered" else "0",
		"caller_id_number": f"+91{CUSTOMER}",
		"call_to_number": f"+91{DID}",
		"did_number": f"+91{DID}",
		"duration": 52,
		"start_stamp": "2026-08-12 17:34:58",
		"end_stamp": "2026-08-12 17:35:49",
		"hangup_cause": "NormalClearing",
		"recording_url": recording_url,
		"answered_agent": [],
	}


class TelephonyRecordingCase(FileLayerCase):
	"""Real operator rows, real blobs. What is committed here is deleted here."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		config.ensure_account()
		cls.saved_rules = config.current_rules()
		cls.addClassCleanup(config.set_rules, cls.saved_rules)
		cls.addClassCleanup(config.clear_dids)
		config.set_rules([config.rule("Inbound", "Dialer")])
		config.clear_dids()
		config.map_did(DID)

	def setUp(self):
		super().setUp()
		self.calls = []
		self.saved_offload = frappe.db.get_value("CRM Tatva Automation", _OFFLOAD, "enabled")
		frappe.db.set_value("CRM Tatva Automation", _OFFLOAD, "enabled", 1)
		frappe.db.commit()

	def tearDown(self):
		for name in self.calls:
			if frappe.db.exists("CRM Call Log", name):
				frappe.delete_doc("CRM Call Log", name, force=True, ignore_permissions=True)
		frappe.db.set_value("CRM Tatva Automation", _OFFLOAD, "enabled", self.saved_offload)
		frappe.db.commit()
		super().tearDown()

	def deliver(self, payload, *, event="inbound_hangup", response=None, guard=False):
		"""Drive the REAL webhook entry with only the producer's HTTP replaced. Returns the row name.

		`guard=False` patches the SSRF check off exactly as `test_call_media.deliver` does, because these
		cases are about what lands in Azure; that the guard really runs is its own test below.
		"""
		with patch("requests.get") as fetch:
			fetch.return_value = response or _FakeResponse()
			if guard:
				name = acefone.process(payload, event=event, account=config.ACCOUNT)
			else:
				with patch("tatva_connect.utils.assert_safe_public_url"):
					name = acefone.process(payload, event=event, account=config.ACCOUNT)
		self.fetch = fetch
		if name:
			self.calls.append(name)
			self._register_keys(name)
		return name

	def _register_keys(self, call):
		for url in frappe.get_all("File", filters={"attached_to_doctype": "CRM Call Log",
		                                           "attached_to_name": call}, pluck="file_url"):
			key = blob_key_from_url(url)
			if key:
				self._keys.append(key)

	def media(self, call):
		return frappe.db.get_value(call_media.MEDIA_DT, call, "*", as_dict=True)


class TestTheAdapterAnswersWhereTheAudioIs(TelephonyRecordingCase):
	"""Three answers and no fourth — the same vocabulary the AI-voice adapter speaks."""

	def test_a_cdr_carrying_a_url_says_here_it_is(self):
		"""138 of the 179 captured CDRs carry `recording_url` on the hangup itself, so this is the path."""
		ref = acefone.recording_ref(_cdr("ref-1"), "Completed", config.ACCOUNT)
		self.assertEqual(ref.url, _PROVIDER_URL)
		self.assertEqual(ref.provider, "Acefone")
		self.assertFalse(ref.pending)
		self.assertFalse(ref.absent)

	def test_a_missed_call_with_no_url_is_settled_not_awaited(self):
		"""41 of the 179 carried none, every one of them missed. Awaiting them would wait for ever."""
		ref = acefone.recording_ref(_cdr("ref-2", recording_url=None, call_status="missed"), "No Answer")
		self.assertTrue(ref.absent)
		self.assertFalse(ref.pending)

	def test_a_call_still_live_is_not_ready_yet(self):
		"""The answered-live trigger fires before the hangup that publishes the recording."""
		ref = acefone.recording_ref(_cdr("ref-3", recording_url=None), "In Progress")
		self.assertTrue(ref.pending)
		self.assertFalse(ref.absent)

	def test_the_ref_carries_the_accounts_credential_and_host_allowlist(self):
		"""The SAME two protections `api.telephony.recording` applies at play time, on the ingest fetch."""
		ref = acefone.ref_for_url(_PROVIDER_URL, config.ACCOUNT)
		self.assertEqual(ref.headers.get("Authorization"), "Bearer resolve-gates-test-token")
		# The allowlist links an operator-curated host, so the vocabulary row comes first.
		host = "recordings.acefone.invalid"
		if not frappe.db.exists("CRM Trusted Fetch Host", host):
			frappe.get_doc({"doctype": "CRM Trusted Fetch Host", "host": host}).insert(
				ignore_permissions=True  # authz-ok: tier-a — test fixture, runs as Administrator
			)
		acct = frappe.get_doc("CRM Telephony Account", config.ACCOUNT)
		acct.append("recording_host_allowlist", {"host": host})
		acct.save(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(self._clear_allowlist, host)
		frappe.clear_cache(doctype="CRM Telephony Account")
		self.assertEqual(
			acefone.ref_for_url(_PROVIDER_URL, config.ACCOUNT).allowed_hosts,
			("recordings.acefone.invalid",),
		)

	def _clear_allowlist(self, host):
		acct = frappe.get_doc("CRM Telephony Account", config.ACCOUNT)
		acct.set("recording_host_allowlist", [])
		acct.save(ignore_permissions=True)
		frappe.delete_doc("CRM Trusted Fetch Host", host, force=True, ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache(doctype="CRM Telephony Account")


class TestTheRecordingLandsInAzure(TelephonyRecordingCase):
	"""The whole path, end to end: webhook -> storage layer -> real blob -> play button."""

	def test_the_webhook_stores_the_bytes_and_the_row_plays_from_our_copy(self):
		call = self.deliver(_cdr("land-1"))
		self.assertIsNotNone(call, "the CDR was declined — the operator rows are not configured")

		row = self.media(call)
		self.assertEqual(row.recording_state, call_media.STORED)
		self.assertEqual(row.recording_source, "Acefone")

		file_doc = frappe.get_doc("File", row.recording_file)
		self.assertEqual(file_doc.attached_to_doctype, "CRM Call Log")
		self.assertEqual(file_doc.attached_to_name, call, "the blob's life must be the call's life")
		self.assertTrue(file_doc.file_name.startswith("acefone_"), file_doc.file_name)
		self.assertTrue(file_doc.file_name.endswith(".mp3"), file_doc.file_name)
		self.assertTrue(file_doc.is_private, "patient audio must land private")

		self.assert_in_azure(blob_key_from_url(file_doc.file_url), "telephony recording")

		played = call_media.media_for(call)["recording"]
		self.assertEqual(played["url"], file_doc.file_url)
		self.assertEqual(played["state"], call_media.STORED)

	def test_the_row_never_keeps_the_providers_url(self):
		"""The rot this fixes: a link to somebody else's host, on the row, played at click time."""
		call = self.deliver(_cdr("land-2"))
		stored = frappe.db.get_value("CRM Call Log", call, "recording_url")
		self.assertNotEqual(stored, _PROVIDER_URL)
		self.assertFalse(stored.startswith("http"), f"still a provider URL: {stored}")
		self.assertEqual(stored, frappe.db.get_value("File", self.media(call).recording_file, "file_url"))

	def test_the_bytes_are_the_bytes_the_provider_served(self):
		"""Byte-for-byte, read back through the file layer — not `upload.called`."""
		call = self.deliver(_cdr("land-3"))
		file_doc = frappe.get_doc("File", self.media(call).recording_file)
		self.assertEqual(file_doc.get_content(), _AUDIO)

	def test_a_redelivery_fetches_nothing_twice(self):
		"""One live CDR arrived eleven times byte for byte; eleven copies must not be eleven downloads."""
		payload = _cdr("land-4")
		call = self.deliver(payload)
		first = self.media(call).recording_file
		self.deliver(payload)
		self.fetch.assert_not_called()
		self.assertEqual(self.media(call).recording_file, first)

	def test_a_missed_call_settles_without_a_fetch(self):
		call = self.deliver(_cdr("land-5", recording_url=None, call_status="missed"))
		self.fetch.assert_not_called()
		self.assertEqual(self.media(call).recording_state, call_media.ABSENT)

	def test_a_provider_that_fails_leaves_the_call_logged_and_the_gap_retryable(self):
		"""A failed fetch is a GAP, not a failed webhook — the call is real and must still be logged."""
		with patch("requests.get", side_effect=OSError("provider down")), \
		     patch("tatva_connect.utils.assert_safe_public_url"):
			call = acefone.process(_cdr("land-6"), event="inbound_hangup", account=config.ACCOUNT)
		self.calls.append(call)
		self.assertTrue(frappe.db.exists("CRM Call Log", call))
		row = self.media(call)
		self.assertEqual(row.recording_state, call_media.AWAITING)
		self.assertEqual(row.recording_ref_url, _PROVIDER_URL)
		self.assertIsNotNone(row.recording_next_attempt_at, "the sweep needs a due time to retry from")

	def test_an_unsafe_recording_host_is_refused_before_the_wire(self):
		"""The guard runs on a telephony URL exactly as it runs on any other producer's."""
		call = self.deliver(
			_cdr("land-7", recording_url="http://169.254.169.254/latest/meta-data/"), guard=True
		)
		self.fetch.assert_not_called()
		self.assertIsNone(self.media(call).recording_file)


class TestLegacyRowsAreAdopted(TelephonyRecordingCase):
	"""The calls already logged — the rows whose audio is still only the provider's."""

	def test_a_legacy_row_is_adopted_and_repointed(self):
		call = self.deliver(_cdr("legacy-1", recording_url=None, call_status="missed"))
		frappe.db.set_value("CRM Call Log", call, {
			"recording_url": _PROVIDER_URL, "custom_telephony_account": config.ACCOUNT,
		})
		frappe.db.delete(call_media.MEDIA_DT, {"call": call})
		frappe.db.commit()

		with patch("requests.get") as fetch, patch("tatva_connect.utils.assert_safe_public_url"):
			fetch.return_value = _FakeResponse()
			summary = reconcile.backfill_recordings(limit=50, dry_run=False)
		self._register_keys(call)

		self.assertGreaterEqual(summary["adopted"], 1)
		row = self.media(call)
		self.assertEqual(row.recording_state, call_media.STORED)
		self.assert_in_azure(blob_key_from_url(
			frappe.db.get_value("File", row.recording_file, "file_url")), "adopted legacy recording")
		self.assertFalse(frappe.db.get_value("CRM Call Log", call, "recording_url").startswith("http"))

	def test_a_dry_run_touches_nothing(self):
		call = self.deliver(_cdr("legacy-2", recording_url=None, call_status="missed"))
		frappe.db.set_value("CRM Call Log", call, "recording_url", _PROVIDER_URL)
		frappe.db.commit()
		with patch("requests.get") as fetch:
			summary = reconcile.backfill_recordings(limit=50)
		fetch.assert_not_called()
		self.assertTrue(summary["dry_run"])
		self.assertEqual(frappe.db.get_value("CRM Call Log", call, "recording_url"), _PROVIDER_URL)


class TestTheWriterIsStillProviderBlind(TelephonyRecordingCase):
	"""The hand-off is DATA. Adding a telephony provider still adds an adapter and nothing else."""

	def test_the_writer_stores_whatever_ref_it_is_handed(self):
		call = self.deliver(_cdr("blind-1", recording_url=None, call_status="missed"))
		with patch("requests.get") as fetch, patch("tatva_connect.utils.assert_safe_public_url"):
			fetch.return_value = _FakeResponse()
			writer.adopt_recording(call, contract.RecordingRef(
				url="https://api.some-other-provider.invalid/rec/9", provider="Ozonetel"
			))
		self._register_keys(call)
		row = self.media(call)
		self.assertEqual(row.recording_source, "Ozonetel")
		self.assertTrue(
			frappe.db.get_value("File", row.recording_file, "file_name").startswith("ozonetel_")
		)

	def test_a_row_with_no_ref_is_left_alone(self):
		"""A manually logged call names no producer; the media layer must not invent state for it."""
		call = self.deliver(_cdr("blind-2", recording_url=None, call_status="missed"))
		frappe.db.delete(call_media.MEDIA_DT, {"call": call})
		writer.adopt_recording(call, None)
		self.assertFalse(frappe.db.exists(call_media.MEDIA_DT, call))
