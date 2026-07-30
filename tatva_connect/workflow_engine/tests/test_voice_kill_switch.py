# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE VOICE MASTER KILL-SWITCH KILLS OUTBOUND TOO — the asymmetry this suite exists to close.

`AI Voice::Channel::calls` gated the INBOUND webhook only. The outbound dial sat behind
`Workflow::Engine::sends` alone, so an operator who switched the voice channel OFF stopped hearing about
calls and went on placing them. WhatsApp has never had that gap — `send_whatsapp` consults
`whatsapp.channel.is_enabled()` on top of the sends gate (sends.py:180) — and voice was the odd one out.

Worse than the gap: `voice/channel.py` SAID it stopped both directions. A docstring claiming a guard that
does not exist is how a real person gets dialled by a switched-off integration.

The proving test is `test_the_channel_switch_alone_stops_the_dial`: with the sends gate ARMED and the
voice channel OFF, nothing may reach the provider. On the old code that combination dialled.

Nothing here dials. `requests.post` is patched at the adapter boundary throughout; asserting it was never
called IS the proof.
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import sends
from tatva_connect.voice import channel

_ACCOUNT = "voice-killswitch-probe"
_GOOD = "+919059067237"
_TOKEN = "run-ks::voice-1"


class TestTheVoiceChannelSwitchStopsOutbound(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.exists("CRM AI Voice Account", _ACCOUNT):
			frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "CRM AI Voice Account", "account_name": _ACCOUNT,
			"api_key": "sk-test-never-real", "enabled": 1,  # pragma: allowlist secret
		}).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _send(self, number=_GOOD):
		"""Send with the SENDS gate armed. The voice channel switch is whatever the test set it to — that
		is the variable under examination. No agent placeholders, so the provider is never asked for slots."""
		with patch("tatva_connect.automation.sends.sends_enabled", return_value=True), \
		     patch("tatva_connect.voice.adapters.bolna.requests.post") as post, \
		     patch("tatva_connect.automation.sends._agent_variables", return_value=({}, [])), \
		     patch("frappe.enqueue") as enqueue:
			output, result = sends.send_voice(
				"LEAD-1", "crm_lead.mobile_no", _ACCOUNT, "agent-1",
				context={"crm_lead.mobile_no": number}, correlation=_TOKEN,
			)
			# The thunk is what a committed segment would run; running it here is what would really dial.
			if callable(result):
				result()
		return output, result, post, enqueue

	# --- the proving test ------------------------------------------------------------------------------

	def test_the_channel_switch_alone_stops_the_dial(self):
		"""SENDS ARMED + VOICE CHANNEL OFF must place nothing. This is the case that dialled before."""
		self.assertFalse(channel.is_enabled(), "the voice channel switch ships OFF; this bench has it on")
		output, result, post, enqueue = self._send()
		self.assertEqual(output, sends.FAILED)
		self.assertIn("switched off", result)
		post.assert_not_called()
		enqueue.assert_not_called()

	def test_with_both_on_the_call_is_deferred_to_the_workflow_lane(self):
		"""The other half: the switch must not be a wall. With both gates open the send behaves exactly as
		before — deferred past commit, never dialled inside the segment."""
		with patch("tatva_connect.voice.channel.is_enabled", return_value=True):
			output, _result, post, enqueue = self._send()
		self.assertEqual(output, sends.PLACED)
		post.assert_not_called()  # deferred, not dialled in-segment
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args[1].get("queue"), "workflow")
		self.assertTrue(enqueue.call_args[1].get("enqueue_after_commit"))

	def test_the_country_code_refusal_still_comes_first(self):
		"""The E.164 refusal sits IN FRONT of both gates and stays there: a number that cannot be known
		correct is refused on its own terms, not because a switch happened to be off."""
		with patch("tatva_connect.voice.channel.is_enabled", return_value=True):
			output, result, post, _enqueue = self._send(number="9059067237")
		self.assertEqual(output, sends.FAILED)
		self.assertIn("country code", result)
		post.assert_not_called()

	def test_the_docstring_promise_is_true_in_both_directions(self):
		"""`voice/channel.py` says the switch governs the channel in EITHER direction. Inbound is the
		spine's `enabled` callback; outbound is the send. Both must read the SAME function."""
		from tatva_connect.voice import webhook

		self.assertIs(webhook.channel.is_enabled, channel.is_enabled)
		self.assertIn("voice", sends.send_voice.__doc__.lower())
