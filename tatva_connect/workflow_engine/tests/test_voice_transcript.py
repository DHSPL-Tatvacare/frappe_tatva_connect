# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""WHAT THE VOICE ADAPTER STILL OWNS, now that the media layer owns the artifacts.

The adapter's whole contribution to a call's media is two answers about ITS OWN payload:

    parse_transcript / transcript_of   what "assistant:" means, in the canonical shape
    recording_ref                      here it is / not ready yet / there is none

Nothing here stores, names, dedupes, retries or serves anything — those live once in
`storage.call_media` and are proven in `tests/storage/test_call_media`. This suite proves the SEAM: that
the terminal callback drives both shared doors, with the adapter's answers and nobody else's.

If a test in this file starts asserting where a file lands or how a row is written, the storage logic has
crept back into the adapter and the whole point of the split is gone.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.workflow_engine.tests.test_voice_transcript
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import sends
from tatva_connect.channels import contract
from tatva_connect.storage import call_media
from tatva_connect.voice.adapters import bolna
from tatva_connect.workflow_engine.tests import fixtures as fx

_ACCOUNT = "voice-transcript-account"
_EXEC = "exec-transcript-0001"
_NUMBER = "+919999999999"
_RECORDING_URL = "https://api.bolna.invalid/recordings/call/" + _EXEC

# What Bolna really sends: one flat string, speaker prefixes inline, no timings.
_BOLNA_TRANSCRIPT = (
	"assistant: Hi Pareekshith\n"
	"user: yes\n"
	"assistant: Your Diabetes Program plan is now active.\n"
)


def _callback(execution_id=_EXEC, status="completed", transcript=_BOLNA_TRANSCRIPT, summary="A short call.",
              recording_url=_RECORDING_URL):
	payload = {
		"id": execution_id,
		"status": status,
		"user_data": {bolna.USER_DATA_CORRELATION_KEY: ""},
		"telephony_data": {"duration": 42, "to_number": _NUMBER, "from_number": "+918035303509"},
	}
	if transcript is not None:
		payload["transcript"] = transcript
	if summary is not None:
		payload["summary"] = summary
	if recording_url:
		payload["telephony_data"]["recording_url"] = recording_url
	return payload


class TestTheAdapterOwnsWhatAssistantMeans(FrappeTestCase):
	"""Pure parsing — no DB. Bolna's flat string with inline speaker prefixes becomes the canonical shape,
	and ONLY this function knows the prefixes exist."""

	def test_a_flat_string_becomes_role_segments(self):
		"""The provider's own words stop HERE. What lands is which side of the call spoke, so no screen
		has to know that this vendor says "assistant" and the next one says "bot"."""
		parsed = bolna.parse_transcript(_BOLNA_TRANSCRIPT)
		self.assertEqual(
			[s["role"] for s in parsed["segments"]],
			[call_media.ROLE_AGENT, call_media.ROLE_CONTACT, call_media.ROLE_AGENT],
		)
		self.assertEqual(parsed["segments"][1]["text"], "yes")

	def test_the_providers_own_word_is_not_stored(self):
		"""A label in a database row is a wording decision baked into data — and it would be wrong the
		moment a screen wants the lead's own name there instead of a generic noun."""
		for segment in bolna.parse_transcript(_BOLNA_TRANSCRIPT)["segments"]:
			self.assertNotIn("speaker", segment)
			self.assertIn(segment["role"], call_media.ROLES)

	def test_the_segments_carry_no_times_because_bolna_gives_none(self):
		"""Sparse, not padded. An absent time is absent, never a zero that would draw a wrong timestamp."""
		for segment in bolna.parse_transcript(_BOLNA_TRANSCRIPT)["segments"]:
			self.assertNotIn("start", segment)
			self.assertNotIn("end", segment)

	def test_plain_text_is_one_segment_with_no_role(self):
		"""The bottom rung: a transcription service returning prose lands here with no new shape."""
		parsed = bolna.parse_transcript("The patient confirmed the appointment.")
		self.assertEqual(len(parsed["segments"]), 1)
		self.assertNotIn("role", parsed["segments"][0])
		self.assertEqual(parsed["text"], "The patient confirmed the appointment.")

	def test_the_plain_text_is_always_present(self):
		"""A reader must never need to understand segments to read the call."""
		self.assertIn("Hi Pareekshith", bolna.parse_transcript(_BOLNA_TRANSCRIPT)["text"])

	def test_an_unknown_prefix_is_left_unattributed_rather_than_guessed(self):
		"""A role this provider has never sent is not invented. The line is kept whole and renders on the
		plain-text rung — a wrong attribution on a clinical call is worse than none."""
		parsed = bolna.parse_transcript("Dr Mehta: the reports look fine.")
		self.assertNotIn("role", parsed["segments"][0])
		self.assertIn("reports look fine", parsed["segments"][0]["text"])

	def test_nothing_in_gives_nothing_out(self):
		self.assertIsNone(bolna.parse_transcript(""))
		self.assertIsNone(bolna.parse_transcript(None))

	def test_the_canonical_shape_carries_provenance_and_the_raw_payload(self):
		"""`transcript_of` is the seam: the parser's output, plus who said it and what they really sent."""
		canonical = bolna.transcript_of(_callback())
		self.assertEqual(canonical["source"], bolna.DECLARATION.provider)
		self.assertIn("Hi Pareekshith", canonical["text"])
		self.assertEqual(len(canonical["segments"]), 3)
		self.assertIn("assistant:", canonical["raw"])

	def test_a_callback_with_nothing_transcribed_has_no_canonical_shape(self):
		self.assertIsNone(bolna.transcript_of(_callback(transcript=None, summary=None)))


class TestTheAdapterSaysWhereTheAudioIs(FrappeTestCase):
	"""`recording_ref` — the whole of the `recording` capability, and three answers with no fourth."""

	def test_the_capability_is_declared(self):
		"""An adapter that cannot say where its audio is must not be asked; the declaration is how the rest
		of the app knows without importing it."""
		self.assertTrue(bolna.DECLARATION.can("recording"))

	def test_a_terminal_callback_with_a_url_says_here_it_is(self):
		ref = bolna.recording_ref(_callback())
		self.assertEqual(ref.url, _RECORDING_URL)
		self.assertEqual(ref.provider, bolna.DECLARATION.provider)
		self.assertFalse(ref.pending)
		self.assertFalse(ref.absent)

	def test_a_call_still_in_flight_says_not_ready_yet(self):
		"""The recording is published during the post-call processing that `completed` marks the end of.
		Answering `absent` here would settle the row and the audio would never arrive."""
		ref = bolna.recording_ref(_callback(status="call-disconnected", recording_url=None))
		self.assertTrue(ref.pending)
		self.assertFalse(ref.absent)

	def test_a_terminal_callback_with_no_url_says_there_is_none(self):
		ref = bolna.recording_ref(_callback(status="no-answer", recording_url=None))
		self.assertTrue(ref.absent)
		self.assertFalse(ref.pending)

	def test_the_adapter_needs_no_authentication_for_its_own_url(self):
		"""Bolna publishes recordings unauthenticated — which is exactly why we pull the bytes in."""
		self.assertEqual(bolna.recording_ref(_callback()).headers, {})


class TestTheCallbackDrivesTheSharedDoors(FrappeTestCase):
	"""THE SEAM. One terminal callback, both doors, no storage logic in the adapter."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.exists("CRM AI Voice Account", _ACCOUNT):
			frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "CRM AI Voice Account", "account_name": _ACCOUNT,
			"api_key": "sk-never-real", "from_phone": "+918035303509", "enabled": 1,
		}).insert(ignore_permissions=True)
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(call_media.MEDIA_DT, {})
		frappe.db.delete("CRM Call Log", {"telephony_medium": bolna.CALL_MEDIUM})
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.db.delete(call_media.MEDIA_DT, {})
		frappe.db.delete("CRM Call Log", {"telephony_medium": bolna.CALL_MEDIUM})
		frappe.db.commit()

	def _place(self, execution_id=_EXEC):
		placed = {"correlation_id": execution_id, "contact": _NUMBER, "mode": "single",
		          "from_phone": "+918035303509", "raw": {}}
		with patch("tatva_connect.voice.adapters.bolna.place_call", return_value=placed):
			sends._deliver_voice(_ACCOUNT, _NUMBER, "agent-1", None, self.lead.name,
			                     correlation="run-t::voice-1")
		return execution_id

	def _deliver(self, **kwargs):
		"""The terminal callback, with the shared doors watched rather than replaced."""
		with patch.object(call_media, "store_transcript", wraps=call_media.store_transcript) as text, \
		     patch.object(call_media, "store_recording", return_value=None) as audio:
			bolna.update_call_log(_callback(**kwargs))
		return text, audio

	def test_the_transcript_goes_through_the_shared_door(self):
		self._place()
		text, _audio = self._deliver()
		text.assert_called_once()
		self.assertEqual(text.call_args.args[0], _EXEC)
		self.assertIn("Hi Pareekshith", text.call_args.args[1]["text"])

	def test_the_recording_goes_through_the_shared_door_with_the_adapters_ref(self):
		self._place()
		_text, audio = self._deliver()
		audio.assert_called_once()
		ref = audio.call_args.args[1]
		self.assertIsInstance(ref, contract.RecordingRef)
		self.assertEqual(ref.url, _RECORDING_URL)

	def test_the_transcript_really_lands_on_the_media_row(self):
		self._place()
		self._deliver()
		row = frappe.db.get_value(call_media.MEDIA_DT, _EXEC, "*", as_dict=True)
		self.assertIsNotNone(row, "the call was transcribed and the CRM kept nothing")
		self.assertEqual(row.transcript_source, "bolna")
		self.assertEqual(row.summary, "A short call.")
		self.assertEqual(len(frappe.parse_json(row.segments)), 3)

	def test_a_callback_with_no_transcript_writes_no_row(self):
		self._place()
		self._deliver(transcript=None, summary=None, recording_url=None)
		self.assertFalse(frappe.db.exists(call_media.MEDIA_DT, _EXEC),
		                 "an empty transcript must not become an empty row")

	def test_a_transcript_for_a_call_we_never_placed_is_not_invented(self):
		self._deliver(execution_id="exec-never-placed")
		self.assertFalse(frappe.db.exists(call_media.MEDIA_DT, "exec-never-placed"))

	def test_the_call_row_points_at_our_file_and_never_at_the_provider(self):
		self._place()
		with patch.object(call_media, "store_recording", return_value="/api/method/ours?file_name=k"):
			bolna.update_call_log(_callback())
		stored = frappe.db.get_value("CRM Call Log", _EXEC, "recording_url")
		self.assertEqual(stored, "/api/method/ours?file_name=k")
		self.assertNotIn("bolna", (stored or "").lower())

	def test_no_transcript_reaches_the_call_log_row(self):
		"""THE PERF RULE. `CRM Call Log` is hot, mixed and upstream; it must not grow a text blob."""
		self._place()
		self._deliver()
		row = frappe.db.get_value("CRM Call Log", _EXEC, "*", as_dict=True)
		for key, value in row.items():
			self.assertNotIn("Hi Pareekshith", str(value or ""), f"the transcript leaked onto {key}")

	def test_a_redelivery_leaves_one_row_and_does_not_churn_it(self):
		self._place()
		self._deliver()
		frappe.db.commit()
		before = frappe.db.get_value(call_media.MEDIA_DT, _EXEC, "modified")
		self._deliver()
		self.assertEqual(frappe.db.count(call_media.MEDIA_DT, {"call": _EXEC}), 1)
		self.assertEqual(frappe.db.get_value(call_media.MEDIA_DT, _EXEC, "modified"), before)

	def test_a_gap_in_the_audio_never_stops_the_transcript_landing(self):
		"""A failed fetch is a gap, not a failed job: the call still closes and the text still arrives."""
		self._place()
		with patch.object(call_media, "store_recording", side_effect=lambda *a, **k: None):
			bolna.update_call_log(_callback())
		self.assertEqual(frappe.db.get_value("CRM Call Log", _EXEC, "status"), "Completed")
		self.assertTrue(frappe.db.exists(call_media.MEDIA_DT, _EXEC))


class TestNoStorageLogicSurvivesInTheAdapter(FrappeTestCase):
	"""The AST-free version of the grep: the adapter must not be able to own a file any more."""

	def test_the_adapter_has_no_private_store(self):
		for gone in ("_store_recording", "_write_transcript", "TRANSCRIPT_DT"):
			self.assertFalse(hasattr(bolna, gone), f"{gone} is still in the adapter")

	def test_the_adapter_source_names_no_file_doctype(self):
		import inspect

		source = inspect.getsource(bolna)
		self.assertNotIn('"File"', source, "the adapter is writing files again")
		self.assertNotIn("file_manager", source)
