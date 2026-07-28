# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE CALL-MEDIA LAYER — three doors, no vendor, and the bytes are really ours.

This suite drives `storage.call_media` directly, because that is where the rules now live. It knows no
provider: every producer in it is a string handed in on a `RecordingRef`, which is exactly how the layer
sees the real ones.

NOTHING IS FAKED EXCEPT THE PRODUCER'S HTTP. These tests upload to the REAL Azure container and assert
the blob is really there — a mocked BlobStore is what let three months of file bugs through, and asserting
`upload.called` proves nothing about whether a consumer can play the audio. Only the outbound `requests.get`
is replaced, so no test reaches anybody's API.

OFFLOAD IS ARMED FOR THE TEST AND UNARMED BY THE ROLLBACK. `Storage::Azure::offload` is infrastructure —
it messages nobody — and the constitution requires file tests to hit real Azure. It is flipped with
`db.set_value` and never committed, so `FrappeTestCase`'s rollback restores the bench exactly.

Azure has no transaction, so the rollback does NOT remove a blob: every test registers its keys and
`FileLayerCase.tearDown` deletes them.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.storage.test_call_media
"""
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from tatva_connect.channels import contract
from tatva_connect.storage import call_media
from tatva_connect.storage.blob_store import blob_key_from_url
from tatva_connect.tests.storage.test_file_layer_registry import FileLayerCase
from tatva_connect.workflow_engine import thresholds

_OFFLOAD = "Storage::Azure::offload"
_PRODUCER = "acme-voice"
_PRODUCER_URL = "https://api.acme.invalid/recordings/call/abcdef"

# "Not due" is a FUTURE attempt time, the way `test_a_row_that_is_not_due_yet_is_left_alone` says it.
def _NOT_DUE():
	return add_to_date(now_datetime(), hours=2)


# A real mp3 frame header plus filler, so what comes back can be compared byte for byte.
_AUDIO = b"\xff\xfb\x90\x64" + b"tatva-call-recording-probe" * 8


class _FakeResponse:
	"""What the producer's HTTP call answers. Streamed, exactly as the real fetcher reads it."""

	def __init__(self, content=_AUDIO, content_type="audio/mpeg", length=None):
		self.content = content
		self.headers = {"Content-Type": content_type}
		if length is not None:
			self.headers["Content-Length"] = str(length)

	def raise_for_status(self):
		return None

	def iter_content(self, chunk_size):
		for start in range(0, len(self.content), chunk_size):
			yield self.content[start:start + chunk_size]


class CallMediaCase(FileLayerCase):
	"""Real blobs, real File rows, real Azure. Offload armed here, restored by the rollback."""

	def setUp(self):
		super().setUp()
		frappe.db.set_value("CRM Tatva Automation", _OFFLOAD, "enabled", 1)
		self.call = self._make_call()

	def _make_call(self):
		"""A real CRM Call Log row. Never committed, so the rollback takes it and its media row away."""
		return frappe.get_doc({
			"doctype": "CRM Call Log", "id": f"media-{frappe.generate_hash(length=10)}",
			"type": "Outgoing", "status": "Initiated", "telephony_medium": "AI Voice",
			"to": "+919999999999", "from": "+918035303509", "duration": 0,
		}).insert(ignore_permissions=True).name

	def deliver(self, ref=None, response=None, error=None, call=None):
		"""Drive `store_recording` with ONLY the producer's HTTP replaced. Returns the mock."""
		ref = ref if ref is not None else contract.RecordingRef(url=_PRODUCER_URL, provider=_PRODUCER)
		with patch("requests.get") as fetch:
			if error is not None:
				fetch.side_effect = error
			else:
				fetch.return_value = response or _FakeResponse()
			self.url = call_media.store_recording(call or self.call, ref)
		self._register_keys(call or self.call)
		return fetch

	def _register_keys(self, call):
		for url in frappe.get_all("File", filters={"attached_to_doctype": "CRM Call Log",
		                                           "attached_to_name": call}, pluck="file_url"):
			key = blob_key_from_url(url)
			if key:
				self._keys.append(key)

	def file_row(self, call=None):
		rows = frappe.get_all(
			"File", filters={"attached_to_doctype": "CRM Call Log", "attached_to_name": call or self.call},
			fields=["name", "file_name", "file_url", "is_private"],
		)
		return rows[0] if rows else None

	def media(self, call=None):
		return frappe.db.get_value(call_media.MEDIA_DT, call or self.call, "*", as_dict=True)


class TestTheBytesBecomeOursAndAreOwnedByTheCall(CallMediaCase):
	"""M1: born owned. A blob's life is exactly its row's life — no folder scheme, no orphans."""

	def test_the_recording_is_stored_as_a_file_on_the_call(self):
		self.deliver()
		self.assertIsNotNone(self.file_row(), "the recording was never pulled into our storage")
		self.assertEqual(self.media().recording_state, call_media.STORED)

	def test_the_blob_is_really_in_the_container(self):
		"""Asserted against REAL Azure, not against a mock having been called."""
		self.deliver()
		key = blob_key_from_url(self.file_row().file_url)
		self.assertTrue(key, "the file never offloaded — its URL is still local")
		self.assertTrue(self.store.exists(key), "the blob is not actually in the container")

	def test_the_bytes_come_back_through_the_file_layer(self):
		"""M2: nothing reads off a disk. `get_content` is the one door and it returns what we stored."""
		self.deliver()
		self.assertEqual(frappe.get_doc("File", self.file_row().name).get_content(), _AUDIO)

	def test_the_recording_is_private(self):
		"""M3 and the ONE privacy checkpoint. `call_media` sets `is_private` nowhere; `CRM Call Log` is not
		on the operator's public allowlist, so the floor holds and no caller may argue with it."""
		self.deliver()
		self.assertEqual(self.file_row().is_private, 1)

	def test_deleting_the_call_removes_the_blob(self):
		"""M1's other half, and the reason there are no orphans: the blob dies with the record."""
		self.deliver()
		key = blob_key_from_url(self.file_row().file_url)
		self.assertTrue(self.store.exists(key))

		frappe.delete_doc("CRM Call Log", self.call, force=True, ignore_permissions=True)
		self.assertFalse(self.store.exists(key), "deleting the call left its recording in the container")

	def test_deleting_the_call_leaves_no_media_row_no_file_and_no_blob(self):
		"""Nothing accumulates for ever. `ignore_links_on_delete` lets the call go so the blob can be
		reclaimed — but for months NOTHING deleted the media row, so every deleted call left an orphan
		pointing at a call that no longer exists. All three must go, in one delete."""
		self.deliver()
		key = blob_key_from_url(self.file_row().file_url)
		file_name = self.file_row().name
		self.assertTrue(frappe.db.exists(call_media.MEDIA_DT, self.call), "no media row to test with")

		frappe.delete_doc("CRM Call Log", self.call, force=True, ignore_permissions=True)

		self.assertFalse(frappe.db.exists(call_media.MEDIA_DT, self.call), "the media row outlived its call")
		self.assertFalse(frappe.db.exists("File", file_name), "the recording's File row outlived its call")
		self.assertFalse(self.store.exists(key), "the blob outlived its call")

	def test_a_row_that_outlived_its_file_is_not_still_stored(self):
		"""`ignore_links_on_delete` lets a call be deleted without its media row blocking the cascade, so
		a row CAN outlive the File it points at. "Stored" with nothing behind it is a lie no sweep could
		correct, and a screen would draw a player over a 404."""
		self.deliver()
		frappe.delete_doc("File", self.file_row().name, force=True, ignore_permissions=True)

		self.assertIsNone(call_media.media_for(self.call)["recording"]["state"])
		self.assertTrue(self.deliver().called, "a lost file must be fetched again, not reported as held")

	def test_a_call_we_do_not_hold_stores_nothing(self):
		self.deliver(call="a-call-this-crm-never-placed")
		self.assertIsNone(self.url)
		self.assertFalse(frappe.db.exists(call_media.MEDIA_DT, "a-call-this-crm-never-placed"))


class TestTheNameSaysWhoProducedIt(CallMediaCase):
	"""A stored recording is looked at in the Azure container and on the lead's Files tab, and in both
	places the only thing that can say where it came from is its name."""

	def test_the_name_carries_producer_call_and_timestamp(self):
		self.deliver()
		name = self.file_row().file_name
		self.assertTrue(name.startswith(f"{frappe.scrub(_PRODUCER)}_"), name)
		self.assertIn(self.call, name)
		self.assertRegex(name, r"_\d{14}\.mp3$")

	def test_the_extension_comes_from_the_content_type_not_the_url(self):
		"""THE RULE. One producer serves audio/mpeg behind a path ending in nothing useful and the next
		will serve wav behind the same shape; a `.mp3` that is not one breaks every player."""
		ref = contract.RecordingRef(url="https://api.acme.invalid/rec/9?fmt=x", provider=_PRODUCER)
		self.deliver(ref=ref, response=_FakeResponse(content_type="audio/wav"))
		self.assertTrue(self.file_row().file_name.endswith(".wav"), self.file_row().file_name)

	def test_an_unrecognised_content_type_still_lands(self):
		"""A producer we have never seen must not lose its bytes over a MIME type nobody mapped."""
		self.deliver(response=_FakeResponse(content_type="application/x-unknown-audio"))
		self.assertTrue(self.file_row().file_name.endswith(call_media._FALLBACK_EXTENSION))

	def test_the_blob_key_builder_is_untouched(self):
		"""`BlobStore.new_key` builds the key for EVERY file in the app. Our name is composed in
		`call_media` and handed to the file layer as an ordinary file name, so the key keeps the shape
		`<app>/<owner_doctype>/<owner_id>/<hash>_<name>` that every other upload has."""
		self.deliver()
		key = blob_key_from_url(self.file_row().file_url)
		parts = key.split("/")
		self.assertEqual(len(parts), 4, key)
		self.assertEqual(parts[1], "crm_call_log")
		self.assertEqual(parts[2], self.call)
		self.assertIn(frappe.scrub(_PRODUCER), parts[3])


class TestTheThreeAnswersStayApart(CallMediaCase):
	"""`pending`, `absent` and a URL are three different facts. Collapsing any pair is how a recording
	silently never arrives, or how a fetcher hammers a URL that does not exist yet."""

	def test_not_ready_yet_parks_the_row_and_fetches_nothing(self):
		fetch = self.deliver(ref=contract.RecordingRef(pending=True, provider=_PRODUCER))
		fetch.assert_not_called()
		row = self.media()
		self.assertEqual(row.recording_state, call_media.AWAITING)
		self.assertIsNone(row.recording_next_attempt_at,
		                  "a pending row must not be due — there is no URL to retry")

	def test_there_is_none_settles_the_row_and_fetches_nothing(self):
		fetch = self.deliver(ref=contract.RecordingRef(provider=_PRODUCER))
		fetch.assert_not_called()
		self.assertEqual(self.media().recording_state, call_media.ABSENT)

	def test_a_url_is_fetched_and_stored(self):
		fetch = self.deliver()
		fetch.assert_called_once()
		self.assertEqual(self.media().recording_state, call_media.STORED)


class TestARedeliveryDownloadsNothing(CallMediaCase):
	"""A producer re-sends: one live CDR arrived ELEVEN times byte for byte. Proven on the ATTEMPT, not on
	the outcome — relying on a unique key to raise would still burn a fetch and an insert on every copy."""

	def test_the_second_delivery_does_not_fetch_again(self):
		self.deliver()
		self.assertFalse(self.deliver().called)

	def test_the_second_delivery_leaves_one_file(self):
		self.deliver()
		self.deliver()
		self.assertEqual(
			frappe.db.count("File", {"attached_to_doctype": "CRM Call Log", "attached_to_name": self.call}), 1
		)

	def test_the_second_delivery_answers_with_the_same_url(self):
		self.deliver()
		first = self.url
		self.deliver()
		self.assertEqual(self.url, first)


class TestAFailedFetchIsAGapNotAFailedJob(CallMediaCase):
	"""The call really happened and its transcript really arrived. Raising here would fail the webhook
	worker and, on its retry, re-dial a patient over a byte fetch."""

	def test_a_failed_fetch_stores_nothing_and_raises_nothing(self):
		self.deliver(error=OSError("the producer is down"))
		self.assertIsNone(self.url)
		self.assertIsNone(self.file_row(), "a failed fetch must not leave a half-made file")

	def test_a_failed_fetch_records_the_reason_and_the_next_attempt(self):
		self.deliver(error=OSError("connection reset"))
		row = self.media()
		self.assertEqual(row.recording_state, call_media.AWAITING)
		self.assertEqual(row.recording_attempts, 1)
		self.assertIn("connection reset", row.recording_error)
		self.assertIsNotNone(row.recording_next_attempt_at)

	def test_the_producers_url_is_kept_so_the_sweep_has_something_to_retry(self):
		self.deliver(error=OSError("nope"))
		self.assertEqual(self.media().recording_ref_url, _PRODUCER_URL)

	def test_a_redelivery_inside_the_backoff_does_not_spend_an_attempt(self):
		"""A producer re-sending eleven copies of one callback must not burn the whole retry budget in a
		second. The backoff governs the immediate re-ask exactly as it governs the sweep."""
		self.deliver(error=OSError("down"))
		fetch = self.deliver()
		fetch.assert_not_called()
		self.assertEqual(self.media().recording_attempts, 1)

	def test_an_abandoned_row_does_not_buy_another_attempt(self):
		for _ in range(call_media.MAX_ATTEMPTS):
			self.deliver(error=OSError("still down"))
			frappe.db.set_value(call_media.MEDIA_DT, self.call, "recording_next_attempt_at", None)
		self.assertEqual(self.media().recording_state, call_media.ABANDONED)
		self.deliver().assert_not_called()

	def test_the_retry_budget_is_finite(self):
		for _ in range(call_media.MAX_ATTEMPTS):
			self.deliver(error=OSError("still down"))
			frappe.db.set_value(call_media.MEDIA_DT, self.call, "recording_next_attempt_at", None)
		row = self.media()
		self.assertEqual(row.recording_state, call_media.ABANDONED)
		self.assertIsNone(row.recording_next_attempt_at, "an abandoned row must never come due again")

	def test_an_oversized_download_is_stopped(self):
		"""The cap is the shared service's, written once for every producer."""
		with patch.object(call_media, "MAX_BYTES", 8):
			self.deliver()
		self.assertIsNone(self.file_row())
		self.assertEqual(self.media().recording_state, call_media.AWAITING)

	def test_an_empty_answer_is_not_a_recording(self):
		self.deliver(response=_FakeResponse(content=b""))
		self.assertIsNone(self.file_row())


class TestTheSweepRetriesWhatWeAreStillOwed(CallMediaCase):
	"""The one genuinely new piece: media has a time dimension, so something has to come back.

	`sweep()` COMMITS per row, deliberately — a worker killed mid-sweep must not re-fetch what it already
	stored. That is right in production and it defeats `FrappeTestCase`'s rollback here, so this class
	cleans up after itself explicitly and puts the switch back exactly as it found it. Never leave a live
	config change behind on a bench.
	"""

	def setUp(self):
		super().setUp()
		# Every test in this class starts DORMANT, whatever an earlier committed run left on the bench.
		frappe.db.set_value("CRM Tatva Automation", call_media.SWEEP_SWITCH, "enabled", 0)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.set_value("CRM Tatva Automation", call_media.SWEEP_SWITCH, "enabled", 0)
		frappe.db.set_value("CRM Tatva Automation", _OFFLOAD, "enabled", 0)
		for name in frappe.get_all("File", filters={"attached_to_doctype": "CRM Call Log",
		                                            "attached_to_name": self.call}, pluck="name"):
			frappe.delete_doc("File", name, force=True, ignore_permissions=True)
		frappe.db.delete(call_media.MEDIA_DT, {"call": self.call})
		frappe.db.delete("CRM Call Log", {"name": self.call})
		frappe.db.commit()
		super().tearDown()

	def _arm(self):
		frappe.db.set_value("CRM Tatva Automation", call_media.SWEEP_SWITCH, "enabled", 1)

	def _due(self, **values):
		call_media._set(self.call, {
			"recording_state": call_media.AWAITING,
			"recording_ref_url": _PRODUCER_URL,
			"recording_source": _PRODUCER,
			"recording_attempts": 1,
			"recording_next_attempt_at": add_to_date(now_datetime(), minutes=-1),
			**values,
		})

	def _sweep(self, response=None):
		with patch("requests.get") as fetch:
			fetch.return_value = response or _FakeResponse()
			swept = call_media.sweep()
		self._register_keys(self.call)
		self.fetch = fetch
		return swept

	def test_it_is_dormant_by_default(self):
		"""Off is not a bug. Every automation in this app ships dormant and this is no exception."""
		self._due()
		self.assertEqual(self._sweep(), 0)
		self.fetch.assert_not_called()

	def test_a_due_row_is_retried_and_stored(self):
		self._arm()
		self._due()
		self.assertEqual(self._sweep(), 1)
		self.assertEqual(self.media().recording_state, call_media.STORED)
		self.assertIsNotNone(self.file_row())

	def test_a_row_that_is_not_due_yet_is_left_alone(self):
		self._arm()
		self._due(recording_next_attempt_at=add_to_date(now_datetime(), hours=2))
		self.assertEqual(self._sweep(), 0)
		self.fetch.assert_not_called()

	def test_a_pending_row_is_never_swept(self):
		"""'Not ready yet' is answered by the producer's NEXT delivery, not by us polling a URL that does
		not exist. It carries no next attempt, so SQL's own NULL semantics keep it out with no second flag."""
		self._arm()
		call_media.store_recording(self.call, contract.RecordingRef(pending=True, provider=_PRODUCER))
		self.assertEqual(self._sweep(), 0)
		self.fetch.assert_not_called()

	def test_an_abandoned_row_is_never_swept(self):
		self._arm()
		self._due(recording_state=call_media.ABANDONED)
		self.assertEqual(self._sweep(), 0)
		self.fetch.assert_not_called()

	def test_a_stored_row_is_never_swept(self):
		self._arm()
		self.deliver()
		self.assertEqual(self._sweep(), 0)
		self.fetch.assert_not_called()

	# ---- the cleanup posture: everything ends, closing comes first ----------------------------
	def _never_resolved(self):
		"""A row parked `Awaiting` by a `pending` ref — no url, no next attempt, exactly as
		`test_a_pending_row_is_never_swept` builds it. This is the shape the retry ladder never reaches."""
		call_media.store_recording(self.call, contract.RecordingRef(pending=True, provider=_PRODUCER))

	def _age_to(self, field, days):
		frappe.db.set_value(
			call_media.MEDIA_DT, self.call, field, add_to_date(now_datetime(), days=-days), update_modified=False
		)
		frappe.db.commit()

	def test_a_row_nothing_ever_resolved_is_closed_not_left_waiting(self):
		"""Closing is the SAFETY act: `_due_rows` asks for `Awaiting`, so an Abandoned row is inert and
		can never buy another fetch — while still being readable, which deleting it would not be."""
		self._arm()
		self._never_resolved()
		self._age_to("creation", thresholds.MEDIA_DEAD_AFTER_DAYS + 1)

		self._sweep()

		self.assertEqual(self.media().recording_state, call_media.ABANDONED, "a row nothing resolved is still Awaiting")
		self.assertTrue(frappe.db.exists(call_media.MEDIA_DT, self.call), "closing must not delete the row")

	def test_a_row_inside_the_dead_age_is_left_alone(self):
		"""The age is a backstop, not a hurry. A young unresolved row is still legitimately waiting."""
		self._arm()
		self._never_resolved()

		self._sweep()

		self.assertEqual(self.media().recording_state, call_media.AWAITING, "a young row was closed early")

	def test_a_terminal_row_past_its_retention_is_deleted(self):
		"""Deleting is housekeeping and comes SECOND — long after the row could still answer a question."""
		self._arm()
		self._due(recording_state=call_media.ABSENT, recording_next_attempt_at=_NOT_DUE())
		self._age_to("modified", thresholds.MEDIA_RETENTION_DAYS + 1)

		self._sweep()

		self.assertFalse(frappe.db.exists(call_media.MEDIA_DT, self.call), "a row past retention was kept")

	def test_a_terminal_row_inside_its_retention_is_kept(self):
		"""The audit trail outlives the behaviour — that is what the long age is for."""
		self._arm()
		self._due(recording_state=call_media.ABSENT, recording_next_attempt_at=_NOT_DUE())

		self._sweep()

		self.assertTrue(frappe.db.exists(call_media.MEDIA_DT, self.call), "a row inside retention was deleted")

	def test_the_reaper_is_dormant_with_the_sweep(self):
		"""One switch for one sweep. A reaper that ran while the switch was off would be a second lane."""
		self._due(recording_state=call_media.ABSENT, recording_next_attempt_at=_NOT_DUE())
		self._age_to("modified", thresholds.MEDIA_RETENTION_DAYS + 1)

		self._sweep()  # switch OFF — setUp leaves it dormant and this test never arms it

		self.assertTrue(frappe.db.exists(call_media.MEDIA_DT, self.call), "a dormant sweep still reaped")


class TestTheTranscriptDoor(CallMediaCase):
	"""ONE way text is ever stored, whoever produced it."""

	def _transcript(self, **over):
		return {"source": "whisper-v3", "text": "Patient confirmed.", "summary": "A short call.",
		        "segments": [{"role": call_media.ROLE_AGENT, "text": "Patient confirmed."}],
		        "raw": '{"x": 1}', **over}

	def test_it_lands_in_the_canonical_shape(self):
		call_media.store_transcript(self.call, self._transcript())
		row = self.media()
		self.assertEqual(row.text, "Patient confirmed.")
		self.assertEqual(row.transcript_source, "whisper-v3")
		self.assertEqual(len(frappe.parse_json(row.segments)), 1)

	def test_a_producer_that_gives_only_prose_still_lands(self):
		"""THE ACCEPTANCE TEST for 'one shape with optional parts' — fewer boxes, never a new shape."""
		call_media.store_transcript(self.call, self._transcript(summary=None, segments=[]))
		row = self.media()
		self.assertEqual(row.text, "Patient confirmed.")
		self.assertFalse(row.summary)
		self.assertEqual(frappe.parse_json(row.segments), [])

	def test_a_second_transcript_replaces_the_first(self):
		"""A re-transcription is a CORRECTION, not history: two texts for one call would only ever be a
		question about which one is the call."""
		call_media.store_transcript(self.call, self._transcript())
		call_media.store_transcript(self.call, self._transcript(
			source="deepgram", text="Patient declined.", raw='{"x": 2}'))
		row = self.media()
		self.assertEqual(row.text, "Patient declined.")
		self.assertEqual(row.transcript_source, "deepgram")
		self.assertEqual(row.raw, '{"x": 2}', "raw must move with the text it explains")
		self.assertEqual(frappe.db.count(call_media.MEDIA_DT, {"call": self.call}), 1)

	def test_the_same_transcript_twice_does_not_touch_the_row(self):
		"""A redelivery is a cheap no-op — eleven copies of one webhook must not churn an indexed row."""
		call_media.store_transcript(self.call, self._transcript())
		before = frappe.db.get_value(call_media.MEDIA_DT, self.call, "modified")
		call_media.store_transcript(self.call, self._transcript())
		self.assertEqual(frappe.db.get_value(call_media.MEDIA_DT, self.call, "modified"), before)

	def test_nothing_transcribed_writes_no_row(self):
		call_media.store_transcript(self.call, {"source": "whisper-v3", "text": "", "summary": None})
		self.assertIsNone(self.media(), "an empty transcript must not become an empty row")

	def test_a_transcript_for_a_call_we_never_placed_is_not_invented(self):
		call_media.store_transcript("no-such-call", self._transcript())
		self.assertFalse(frappe.db.exists(call_media.MEDIA_DT, "no-such-call"))

	def test_the_transcript_never_reaches_the_call_log_row(self):
		"""THE PERF RULE. `CRM Call Log` is hot, mixed and upstream; it must not grow a text blob."""
		call_media.store_transcript(self.call, self._transcript())
		row = frappe.db.get_value("CRM Call Log", self.call, "*", as_dict=True)
		for key, value in row.items():
			self.assertNotIn("Patient confirmed", str(value or ""), f"the transcript leaked onto {key}")


class TestMediaForIsTheOneQuestionAScreenAsks(CallMediaCase):
	"""One door out. Playback is stored-or-nothing, and this is where that is enforced."""

	def test_a_call_with_nothing_answers_with_nothing(self):
		answer = call_media.media_for(self.call)
		self.assertIsNone(answer["recording"])
		self.assertIsNone(answer["transcript"])

	def test_a_stored_recording_answers_with_our_own_url(self):
		self.deliver()
		recording = call_media.media_for(self.call)["recording"]
		self.assertEqual(recording["state"], call_media.STORED)
		self.assertEqual(recording["url"], self.file_row().file_url)
		self.assertEqual(recording["source"], _PRODUCER)

	def test_it_never_answers_with_the_producers_url(self):
		"""THE PRODUCT DECISION, enforced. Patient audio does not play off somebody else's host, however
		unguessable the link — if the bytes are not ours the screen says so."""
		self.deliver(error=OSError("down"))
		answer = call_media.media_for(self.call)
		self.assertIsNone(answer["recording"]["url"])
		self.assertEqual(frappe.db.get_value(call_media.MEDIA_DT, self.call, "recording_ref_url"),
		                 _PRODUCER_URL, "the row must still hold it, for the retry")
		self.assertNotIn(_PRODUCER_URL, frappe.as_json(answer))

	def test_the_transcript_comes_back_parsed(self):
		call_media.store_transcript(self.call, {"source": "whisper-v3", "text": "Hello.",
		                                        "segments": [{"text": "Hello."}]})
		transcript = call_media.media_for(self.call)["transcript"]
		self.assertEqual(transcript["text"], "Hello.")
		self.assertEqual(transcript["segments"], [{"text": "Hello."}])

	def test_visibility_is_decided_by_the_call(self):
		"""I1: a sub-entity is visible exactly when its parent is. There is no second permission story."""
		with patch("frappe.has_permission") as permitted:
			call_media.media_for(self.call)
		permitted.assert_called_once()
		self.assertEqual(permitted.call_args.args[:2], ("CRM Call Log", "read"))
