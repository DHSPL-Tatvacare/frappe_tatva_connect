# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""AI VOICE (Bolna) — pass 1: the outcomes contract, the node shape, and the dormant, non-dialling send.

This chunk ports the evals Bolna adapter's DECLARATION and its classification logic into OUR channel
narrative, declares the AI Voice Call node, and wires a send path that is dormant by default and — by
three independent guards (the send switch is OFF, no `CRM AI Voice Account` doctype exists, and the
declaration resolves its account lazily) — CANNOT place a call. The live `POST /call`, the account
doctype, the webhook and the inspector are pass 2.

THE ACCEPTANCE BAR: a Wait after the voice node offers exactly the declared voice outcomes minus the
node's synchronous outputs, from ONE source (the adapter's declaration) — nothing typed by hand.
"""
import unittest
from typing import ClassVar

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, sends
from tatva_connect.channels import resolve
from tatva_connect.voice.adapters import bolna


class TestTheVoiceOutcomesFlowDown(FrappeTestCase):
	"""THE bar. `outcomes_of` reads the channel's declaration and subtracts the node's synchronous
	outputs — so a downstream Wait offers `answered · completed · no_answer` and nothing more, because
	that is what the adapter DECLARED it can truthfully report, minus the `placed`/`failed` it answers
	synchronously."""

	def test_a_wait_after_voice_offers_exactly_the_declared_outcomes_minus_synchronous(self):
		self.assertEqual(
			sorted(actions.outcomes_of("AI Voice Call")),
			["voice.answered", "voice.completed", "voice.no_answer"],
		)

	def test_the_channel_reports_exactly_what_the_adapter_declared(self):
		self.assertEqual(
			sorted(resolve.outcomes_for_channel("voice")),
			["voice.answered", "voice.completed", "voice.failed", "voice.no_answer"],
		)

	def test_the_outcome_names_live_only_in_the_declaration(self):
		"""ONE source: the node names a channel, never a list; the declaration is the only place the voice
		outcome set is written."""
		verb = actions.VERBS["AI Voice Call"]
		self.assertEqual(verb.get("outcomes_channel"), "voice")
		self.assertNotIn("outcomes", verb, "the node must not carry a second, hand-typed outcome list")
		self.assertEqual(bolna.DECLARATION.outcomes, frozenset({"answered", "no_answer", "completed", "failed"}))


class TestTheNodeIsDeclaredLikeSendWhatsApp(FrappeTestCase):
	def test_the_synchronous_outputs_are_placed_and_failed(self):
		self.assertEqual(actions.VERBS["AI Voice Call"]["outputs"], [sends.PLACED, sends.FAILED])

	def test_placed_is_accepted_for_dialling_never_answered(self):
		"""`placed` means we handed it to the provider — never that a human picked up. `answered` is a
		later, channel-reported outcome, and the two must not be conflated."""
		self.assertEqual(sends.PLACED, "placed")
		self.assertNotIn("placed", bolna.DECLARATION.outcomes)


class TestTheVoiceSendIsDormantAndNeverDials(FrappeTestCase):
	"""The send ships OFF (`Workflow::Engine::sends`). While dormant it records a suppression marker and
	touches NO adapter — the engine runs end to end and no call is placed."""

	def test_the_sends_switch_is_off_on_this_bench(self):
		self.assertFalse(sends.sends_enabled(), "the sends switch is armed — it ships OFF, voice included")

	def test_a_dialable_number_while_dormant_is_suppressed_placed(self):
		output, marker = sends.send_voice("LEAD-1", "crm_lead.mobile_no", "acct", "agent-1",
		                                  context={"crm_lead.mobile_no": "+919059067237"})
		self.assertEqual(output, sends.PLACED)
		self.assertEqual(marker, sends.DORMANT_MARKER)


class TestConformRefusesAnUncountriedNumber(FrappeTestCase):
	"""THE Turkey-disaster prevention, voice form: a number with no country code cannot be known correct,
	so it is REFUSED and routed to `failed` — never dialled. The ONE brain is `number_format=E164_PLUS`
	and `conform_number`, exactly as WhatsApp, and the OPPOSITE of WATI's bare-digit `E164_PLAIN`."""

	def test_a_bare_ten_digit_number_takes_failed_and_dials_nothing(self):
		output, marker = sends.send_voice("LEAD-1", "crm_lead.mobile_no", "acct", "agent-1",
		                                  context={"crm_lead.mobile_no": "9059067237"})
		self.assertEqual(output, sends.FAILED)
		self.assertIn("country code", marker)

	def test_the_declaration_is_the_one_brain_and_the_plus_is_signal(self):
		self.assertEqual(bolna.DECLARATION.number_format, "e164_plus")
		self.assertEqual(bolna.DECLARATION.conform_number("+919059067237"), "+919059067237")
		self.assertIsNone(bolna.DECLARATION.conform_number("9059067237"), "no country code → refused")


class TestClassificationIsPortedVerbatim(unittest.TestCase):
	"""Constraint 2: the classification core is copied AS-IS in logic — same status tokens, same
	_NO_REACH_TOKENS, same safe-default-to-failed. A wrong outcome map routes a real call's result down
	the wrong branch, so it is locked over the (status, reason) → outcome pairs."""

	_CASES: ClassVar[list] = [
		# (status, reason, classify_outcome, canonical, event_name)
		("completed", "", "bolna_answered", "answered", "voice.answered"),
		("answered", "", "bolna_answered", "answered", "voice.answered"),
		("success", "", "bolna_answered", "answered", "voice.answered"),
		("completed", "no-answer", "bolna_rnr", "no_answer", "voice.no_answer"),
		("no-answer", "", "bolna_rnr", "no_answer", "voice.no_answer"),
		("rnr", "", "bolna_rnr", "no_answer", "voice.no_answer"),
		("busy", "", "bolna_rnr", "no_answer", "voice.no_answer"),
		("balance-low", "", "bolna_failed", "failed", "voice.failed"),
		("failed", "", "bolna_failed", "failed", "voice.failed"),
		("error", "", "bolna_failed", "failed", "voice.failed"),
		("stopped", "", "bolna_failed", "failed", "voice.failed"),
		("balance-low", "", "bolna_failed", "failed", "voice.failed"),
		(None, None, "bolna_failed", "failed", "voice.failed"),
	]

	def test_status_reason_maps_to_the_ported_outcome(self):
		for status, reason, action_type, canonical, event in self._CASES:
			with self.subTest(status=status, reason=reason):
				self.assertEqual(bolna.classify_outcome(status, reason), action_type)
				self.assertEqual(bolna._canonical_outcome(action_type), canonical)
				self.assertEqual(bolna.voice_event_name(canonical), event)

	def test_is_terminal_matches_the_vendor_documented_status_set(self):
		"""Bolna's own status reference: "Only `completed` is the final status for every conversation."

		The rest below are the statuses where NO conversation happens, so no post-call processing follows
		and no `completed` ever arrives — they must stay terminal or those journeys park for ever.
		"""
		for terminal in ("completed", "no-answer", "busy", "balance-low", "canceled", "failed", "stopped", "error"):
			self.assertTrue(bolna.is_terminal(terminal), terminal)
		for non_terminal in ("scheduled", "queued", "rescheduled", "initiated", "ringing", "in-progress", "", None):
			self.assertFalse(bolna.is_terminal(non_terminal), non_terminal)

	def test_call_disconnected_is_not_terminal(self):
		"""LIVE DEFECT, four calls, four for four. `call-disconnected` means the audio ended; `completed`
		follows 2-3 minutes later once recording and extraction finish. Treated as terminal it fired FIRST
		and classified `no_answer`, so a journey parked on `voice.completed` woke early carrying "nobody picked
		up" for a call the patient had answered and talked through. Screened out, the journey gets the truth."""
		self.assertFalse(bolna.is_terminal("call-disconnected"))
		wanted, reason = bolna.screen({"status": "call-disconnected", "user_data": {"recipient_id": "r::n"}}, None, "acct")
		self.assertFalse(wanted)
		self.assertIn("not terminal", reason)

	def test_a_completed_call_resumes_the_coarse_done_event_and_its_specific_outcome(self):
		self.assertEqual(
			bolna.voice_resume_event_names("answered"),
			frozenset({"voice.answered", "voice.completed"}),
		)
