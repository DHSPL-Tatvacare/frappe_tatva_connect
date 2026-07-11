"""The Acefone adapter, tested against 179 REAL CDRs.

`fixtures/acefone_cdr_corpus.jsonl` is a live capture (2026-07-11, 19 DIDs, 76 minutes),
anonymised — fake numbers, `rep@example.com`, redacted IVR names, stripped recording tokens —
but structurally untouched. Every property asserted below was observed in production traffic,
not read out of Acefone's documentation. That distinction matters: the previous adapter WAS
written from the documentation, and the capture disproved three of its core assumptions.

These tests are pure `normalize()` — no DB, no site. They pin the parse. The writer's DB moves
are exercised separately.
"""
import json
import os
import unittest

from tatva_connect.telephony import envelope as env
from tatva_connect.telephony.adapters import acefone

CORPUS = os.path.join(os.path.dirname(__file__), "fixtures", "acefone_cdr_corpus.jsonl")


def _corpus():
	with open(CORPUS) as fh:
		return [json.loads(line) for line in fh if line.strip()]


class TestAcefoneCorpus(unittest.TestCase):
	"""Replay the whole capture. If a change breaks the parse, it breaks here."""

	@classmethod
	def setUpClass(cls):
		cls.payloads = _corpus()
		cls.cdrs = [acefone.normalize(p, event="inbound_complete") for p in cls.payloads]

	def test_corpus_is_the_real_capture(self):
		self.assertEqual(len(self.payloads), 179)

	def test_every_cdr_normalizes(self):
		"""No CDR in real traffic is unparseable. A None here means a shape we cannot key."""
		self.assertTrue(all(c is not None for c in self.cdrs))

	def test_core_envelope_fields_always_fill(self):
		"""These filled on 179/179 in the capture. Anything less is a regression."""
		for field in ("call_key", "direction", "channel", "customer_number", "did_number",
		              "status", "started_at", "ended_at"):
			missing = [c for c in self.cdrs if not c[field]]
			self.assertEqual(missing, [], f"{field} empty on {len(missing)} CDRs")

	def test_phone_numbers_are_ten_digits(self):
		"""The provider sends the same number two ways — bare on IVR calls, +91-prefixed on
		Dialer calls. Both must collapse to the same 10 digits at the envelope boundary."""
		for c in self.cdrs:
			self.assertEqual(len(c["customer_number"]), 10, c["call_key"])
			self.assertEqual(len(c["did_number"]), 10, c["call_key"])
			self.assertTrue(c["customer_number"].isdigit())

	def test_both_payload_dialects_are_present_and_handled(self):
		"""Acefone speaks two dialects on ONE webhook. If the corpus ever stops containing
		both, this test is no longer proving the thing it exists to prove."""
		channels = {c["channel"] for c in self.cdrs}
		self.assertEqual(channels, {"IVR", "Dialer"})

	def test_every_answered_call_resolves_an_agent_email(self):
		"""8/8 in the capture. The email lives inside the `answered_agent` ARRAY — NOT in
		`answered_agent_email` (no such variable), NOT in `answered_agent_number` (an
		extension), NOT in `answered_agent_name` (a first name)."""
		answered = [c for c in self.cdrs if c["connected"]]
		self.assertEqual(len(answered), 8)
		for c in answered:
			self.assertEqual(c["agent_key"], "rep@example.com", c["call_key"])

	def test_only_dialer_calls_are_ever_answered(self):
		"""Every human-answered call in 179 was Dialer-routed; the IVR path answered 0 of 139.
		This is why the capture policy starts with the Dialer channel."""
		for c in self.cdrs:
			if c["connected"]:
				self.assertEqual(c["channel"], "Dialer", c["call_key"])

	def test_status_maps_without_guessing(self):
		answered = {c["status"] for c in self.cdrs if c["raw"]["call_status"] == "answered"}
		missed = {c["status"] for c in self.cdrs if c["raw"]["call_status"] == "missed"}
		self.assertEqual(answered, {"Completed"})
		self.assertEqual(missed, {"No Answer"})

	def test_provider_re_sends_calls_so_the_key_must_dedupe(self):
		"""The capture holds 10 repeat CDRs. `call_key` is stable across them — that is what
		makes the row idempotent."""
		keys = [c["call_key"] for c in self.cdrs]
		self.assertEqual(len(keys) - len(set(keys)), 10)

	def test_no_correlation_id_ever_comes_back(self):
		"""Acefone echoes nothing: `ref_id` was empty on all 179. Outbound correlation therefore
		cannot rely on it — the writer falls back to number+recency. If this ever starts
		failing, Acefone changed and outbound correlation just got easier."""
		self.assertTrue(all(c["correlation_key"] is None for c in self.cdrs))

	def test_direction_comes_from_the_payload_not_the_url(self):
		"""Feed every CDR through the OPPOSITE URL trigger. Direction must not budge — it is
		read from the body. Under the old design this silently inverted `from`/`to`."""
		for payload, truth in zip(self.payloads, self.cdrs):
			flipped = acefone.normalize(payload, event="outbound_complete")
			self.assertEqual(flipped["direction"], truth["direction"], truth["call_key"])
			self.assertEqual(flipped["customer_number"], truth["customer_number"])


class TestAcefoneParsing(unittest.TestCase):
	"""The specific things the live capture taught us, pinned individually."""

	def test_both_timestamp_formats_parse(self):
		"""IVR calls stamp '2026-07-11 20:39:28'; Dialer calls stamp '7/11/2026, 9:12:56 PM'.
		Same webhook, same account, same call type."""
		ivr = env.parse_timestamp("2026-07-11 20:39:28")
		dialer = env.parse_timestamp("7/11/2026, 9:12:56 PM")
		self.assertEqual((ivr.year, ivr.month, ivr.day, ivr.hour), (2026, 7, 11, 20))
		self.assertEqual((dialer.year, dialer.month, dialer.day, dialer.hour), (2026, 7, 11, 21))

	def test_unparseable_timestamp_is_none_not_a_guess(self):
		self.assertIsNone(env.parse_timestamp("not a date"))

	def test_short_number_is_rejected_rather_than_suffix_matched(self):
		"""A garbage number must NOT become a 2-digit suffix: `mobile_no LIKE '%5'` matches a
		large slice of the lead table."""
		self.assertEqual(env.phone_digits("55"), "")
		self.assertEqual(env.phone_digits("+91 99112 32686"), "9911232686")
		self.assertEqual(env.phone_digits("9911232686"), "9911232686")

	def test_agent_extension_is_never_treated_as_a_phone(self):
		"""`Extension-0602141810347` is an agent extension. The old adapter fed it to a phone
		matcher; its digits could suffix-collide with a real number."""
		cdr = acefone.normalize({
			"call_id": "x", "direction": "Dialer (inbound)", "call_status": "answered",
			"caller_id_number": "+919911232686", "call_to_number": "+919240276210",
			"answered_agent_number": "Extension-0602141810347",
			"answered_agent_name": "Rep",
		}, event="inbound_complete")
		self.assertIsNone(cdr["agent_key"])

	def test_transferred_call_attributes_to_the_final_agent(self):
		"""On a transfer `answered_agent` carries every agent that touched the call; the rep who
		actually handled it is the last one."""
		cdr = acefone.normalize({
			"call_id": "x", "direction": "Dialer (inbound)", "call_status": "answered",
			"caller_id_number": "+919911232686", "call_to_number": "+919240276210",
			"answered_agent": [
				{"name": "First", "email": "first@example.com", "is_transferred_agent": "No"},
				{"name": "Final", "email": "final@example.com", "is_transferred_agent": "Yes"},
			],
		}, event="inbound_complete")
		self.assertEqual(cdr["agent_key"], "final@example.com")

	def test_cdr_without_a_key_is_dropped_not_invented(self):
		self.assertIsNone(acefone.normalize({"call_status": "missed"}, event="inbound_complete"))
