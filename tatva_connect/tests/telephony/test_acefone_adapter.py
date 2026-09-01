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

from tatva_connect import phone
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
		self.assertTrue(all(c["correlation_keys"] == () for c in self.cdrs))

	def test_direction_comes_from_the_payload_not_the_url(self):
		"""Feed every CDR through the OPPOSITE URL trigger. Direction must not budge — it is
		read from the body. Under the old design this silently inverted `from`/`to`."""
		for payload, truth in zip(self.payloads, self.cdrs, strict=True):
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
		self.assertEqual(phone.match_digits("55", last=10), "")
		self.assertEqual(phone.match_digits("+91 99112 32686", last=10), "9911232686")
		self.assertEqual(phone.match_digits("9000300202", last=10), "9000300202")

	def test_agent_extension_is_never_treated_as_a_phone(self):
		"""`Extension-0602141810347` is an agent extension. The old adapter fed it to a phone
		matcher; its digits could suffix-collide with a real number."""
		cdr = acefone.normalize({
			"call_id": "x", "direction": "Dialer (inbound)", "call_status": "answered",
			"caller_id_number": "+919000300202", "call_to_number": "+919240276210",
			"answered_agent_number": "Extension-0602141810347",
			"answered_agent_name": "Rep",
		}, event="inbound_complete")
		self.assertIsNone(cdr["agent_key"])
		# Carried as a SEAT, matched whole against the agent table — never fed to a phone matcher.
		self.assertEqual(cdr["agent_extension"], "0602141810347")

	def test_transferred_call_attributes_to_the_final_agent(self):
		"""On a transfer `answered_agent` carries every agent that touched the call; the rep who
		actually handled it is the last one."""
		cdr = acefone.normalize({
			"call_id": "x", "direction": "Dialer (inbound)", "call_status": "answered",
			"caller_id_number": "+919000300202", "call_to_number": "+919240276210",
			"answered_agent": [
				{"name": "First", "email": "first@example.com", "is_transferred_agent": "No"},
				{"name": "Final", "email": "final@example.com", "is_transferred_agent": "Yes"},
			],
		}, event="inbound_complete")
		self.assertEqual(cdr["agent_key"], "final@example.com")

	def test_click_to_call_is_outbound_and_keeps_its_numbers_the_right_way_round(self):
		"""THE live outbound payload, 2026-09-01. `direction` is `clicktocall` — a word absent from
		Acefone's docs and from the 2026-07 capture. The old parser matched only on the substring
		"outbound", so this read as INBOUND, `_numbers` swapped customer and DID, and every
		click-to-call was declined as "DID <the customer's mobile> is not mapped to a grain"."""
		cdr = acefone.normalize({
			"call_id": "SRVINF-BGN-SRV108-T15-1788265388.67524", "direction": "clicktocall",
			"call_status": "answered", "caller_id_number": "8065992471",
			"call_to_number": "919059067327", "custom_identifier": "eb5b9d1e2c63",
			"ref_id": "35658efc-c63f-48da-aeb1-1cccadd9cf8c",
		}, event="outbound_complete")
		self.assertEqual(cdr["direction"], "outbound")
		self.assertEqual(cdr["channel"], "Dialer")
		self.assertEqual(cdr["did_number"], "8065992471")
		self.assertEqual(cdr["customer_number"], "9059067327")
		# BOTH ids are offered: the bridge stamps one of them on the row and which one is not knowable here.
		self.assertEqual(cdr["correlation_keys"],
		                 ("eb5b9d1e2c63", "35658efc-c63f-48da-aeb1-1cccadd9cf8c"))

	def test_unknown_direction_defers_to_the_url_trigger_never_to_a_guess(self):
		"""The failure mode `clicktocall` exposed: an unrecognised word must not silently pick a
		direction, because picking wrong inverts `from`/`to` and drops the call."""
		for event, expected in (("outbound_complete", "outbound"), ("inbound_complete", "inbound")):
			cdr = acefone.normalize({
				"call_id": "x", "direction": "some-new-word-acefone-invented",
				"call_status": "answered", "caller_id_number": "8065992471",
				"call_to_number": "919059067327",
			}, event=event)
			self.assertEqual(cdr["direction"], expected, event)

	def test_agent_seat_identifies_the_rep_when_no_email_is_sent(self):
		"""`answered_agent` on the 2026 account is an OBJECT carrying a seat and no email at all, so
		email-only resolution leaves every call unattributed. The seat is carried instead, and it is
		the same value the operator already stores to place that rep's calls."""
		cdr = acefone.normalize({
			"call_id": "x", "direction": "clicktocall", "call_status": "answered",
			"caller_id_number": "8065992471", "call_to_number": "919059067327",
			"answered_agent": {"agent_number": "+919611706150", "id": "0502417430016",
			                   "name": "Revathi-Extension", "number": "0602417430016"},
			"answered_agent_number": "0602417430016",
		}, event="outbound_complete")
		self.assertIsNone(cdr["agent_key"])
		self.assertEqual(cdr["agent_extension"], "0602417430016")

	def test_cdr_without_a_key_is_dropped_not_invented(self):
		self.assertIsNone(acefone.normalize({"call_status": "missed"}, event="inbound_complete"))
