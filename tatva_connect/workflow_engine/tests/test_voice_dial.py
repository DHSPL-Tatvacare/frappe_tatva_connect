# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""AI VOICE (Bolna) — pass 2: the path that DIALS. Every Bolna call here is MOCKED; nothing reaches
api.bolna.ai, and the send switch stays OFF the whole pass.

TWO SAFETY TESTS COME FIRST and never go red: a dormant send places nothing (no `requests.post` reached),
and a number with no country code is refused (routed to `failed`) before any dial. A real call is the
same wrong-country defect with audio; these are the guards that make it impossible.

Correlation rides Bolna's `user_data` echo (settled): the OPAQUE engine token (`run::node`, no PII) goes
into `user_data` at `place_call`, Bolna echoes it on the terminal webhook, and the webhook wakes THAT
parked journey — no lookup row, no commit-race. `execution_id` is kept for audit only.
"""
from unittest.mock import MagicMock, patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import sends
from tatva_connect.voice.adapters import bolna

_ACCOUNT = "voice-dial-probe"
_GOOD = "+919876543210"
_BARE = "9876543210"
_TOKEN = "run-abc::voice-1"


def _mock_ok(execution_id="exec-123"):
	resp = MagicMock()
	resp.status_code = 200
	resp.content = b"{}"
	resp.json.return_value = {"execution_id": execution_id}
	return resp


class TestTheSafetyGuardsDialNothing(FrappeTestCase):
	"""These two never go red. `requests.post` is patched at the adapter boundary; asserting it is NOT
	called is the proof that no call left the building."""

	def test_the_sends_switch_is_off(self):
		self.assertFalse(sends.sends_enabled(), "the sends switch is armed — it ships OFF, voice included")

	def test_a_dormant_send_places_nothing(self):
		with patch("tatva_connect.voice.adapters.bolna.requests.post") as post:
			output, marker = sends.send_voice("LEAD-1", "crm_lead.mobile_no", _ACCOUNT, "agent-1",
			                                  context={"crm_lead.mobile_no": _GOOD}, correlation=_TOKEN)
		self.assertEqual(output, sends.PLACED)
		self.assertEqual(marker, sends.DORMANT_MARKER)
		post.assert_not_called()

	def test_a_bare_uncountried_number_is_refused_and_dials_nothing(self):
		with patch("tatva_connect.voice.adapters.bolna.requests.post") as post:
			output, marker = sends.send_voice("LEAD-1", "crm_lead.mobile_no", _ACCOUNT, "agent-1",
			                                  context={"crm_lead.mobile_no": _BARE}, correlation=_TOKEN)
		self.assertEqual(output, sends.FAILED)
		self.assertIn("country code", marker)
		post.assert_not_called()


class TestTheLiveCallIsDeferredAndCarriesTheEngineToken(FrappeTestCase):
	"""The armed path (switch patched ON — the DB switch is never touched) defers the provider call past
	commit on the `workflow` lane, and the deferred `_deliver_voice` POSTs a SINGLE /call carrying the
	engine token in `user_data`, the recipient conformed to +E.164. All mocked."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.exists("CRM AI Voice Account", _ACCOUNT):
			frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)
		cls.account = frappe.get_doc({
			"doctype": "CRM AI Voice Account", "account_name": _ACCOUNT,
			"api_key": "sk-test-never-real", "base_url": "https://api.bolna.ai", "enabled": 1,  # pragma: allowlist secret
		}).insert(ignore_permissions=True).name

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)
		super().tearDownClass()

	def test_an_armed_send_defers_the_call_on_the_workflow_lane(self):
		# BOTH gates armed: the sends switch and the voice channel's own master switch. Arming only the
		# first used to be enough, which was the asymmetry `test_voice_kill_switch` exists to hold shut.
		# `_agent_variables` is patched so no test ever reaches out to the real provider for slot names.
		with patch("tatva_connect.automation.sends.sends_enabled", return_value=True), \
		     patch("tatva_connect.voice.channel.is_enabled", return_value=True), \
		     patch("tatva_connect.automation.sends._agent_variables", return_value=({}, [])), \
		     patch("tatva_connect.voice.adapters.bolna.requests.post") as post, \
		     patch("frappe.enqueue") as enqueue:
			output, thunk = sends.send_voice("LEAD-1", "crm_lead.mobile_no", _ACCOUNT, "agent-1",
			                                 context={"crm_lead.mobile_no": _GOOD}, correlation=_TOKEN)
			self.assertEqual(output, sends.PLACED)
			thunk()  # what dispatcher.run_effects runs after the segment commits
		post.assert_not_called()  # the call is DEFERRED, not made in the segment
		enqueue.assert_called_once()
		_, kwargs = enqueue.call_args
		self.assertEqual(kwargs.get("queue"), "workflow")
		self.assertTrue(kwargs.get("enqueue_after_commit"))
		self.assertIn("_deliver_voice", enqueue.call_args[0][0])

	def test_deliver_voice_posts_a_single_call_with_the_engine_token_and_no_live_dial(self):
		with patch("tatva_connect.voice.adapters.bolna.requests.post", return_value=_mock_ok("exec-xyz")) as post:
			sends._deliver_voice(_ACCOUNT, _GOOD, "agent-1", None, "LEAD-1", correlation=_TOKEN)
		post.assert_called_once()
		url = post.call_args[0][0]
		body = post.call_args[1]["json"]
		self.assertTrue(url.endswith("/call"), url)
		self.assertEqual(body["recipient_phone_number"], _GOOD)
		self.assertEqual(body["agent_id"], "agent-1")
		self.assertEqual(body["user_data"]["recipient_id"], _TOKEN, "the engine token must ride user_data for the webhook echo")
		# NO LIVE DIAL: `requests.post` is a MagicMock (patched at the adapter boundary), so the URL above is
		# only where the call WOULD go — nothing reached api.bolna.ai. The mock IS the proof.
		self.assertTrue(hasattr(post, "assert_called_once"), "requests.post must be the patched mock, never the real network call")


class TestPlaceCallRefusesAResponseThatCannotBeCorrelated(FrappeTestCase):
	"""A /call answer with no execution_id cannot be correlated to a webhook — Bolna is asked to fail loud
	rather than place a call whose outcome can never wake the journey."""

	def test_a_response_without_execution_id_raises(self):
		blank = MagicMock(status_code=200, content=b"{}")
		blank.json.return_value = {}
		with patch("tatva_connect.voice.adapters.bolna.requests.post", return_value=blank):
			with self.assertRaises(bolna.BolnaServiceError):
				bolna.place_call({"api_key": "k", "base_url": "https://api.bolna.ai"}, _GOOD, "agent-1", None, _TOKEN)


class TestADialThatGotNoAnswerIsUnknownAndNeverReplayed(FrappeTestCase):
	"""A6 — voice had no unknown class, so a dial that never got an answer was indistinguishable from one
	Bolna refused. It propagated raw, the job landed on the RQ failed registry, and the recovery action
	that bin offers is REPLAY — which dials a patient who may already have been ringing.

	WhatsApp built `transport.OutcomeUnknown` for exactly this and voice never inherited it. Nothing about
	the graph changes: `placed`/`failed` was decided synchronously in `send_voice`, and this job runs after.
	"""

	def setUp(self):
		# The credential read is not what is under test and owns no account row here; the wire below is.
		connection = patch("tatva_connect.voice.api.connection_for", return_value={
			"api_key": "never-real", "base_url": "https://api.bolna.ai", "from_phone": "",
		})
		connection.start()
		self.addCleanup(connection.stop)

	def _no_answer(self):
		return patch("tatva_connect.voice.adapters.bolna.requests.post",
		             side_effect=requests.Timeout("read timed out"))

	def test_a_dial_with_no_answer_raises_the_unknown_and_not_a_service_error(self):
		with self._no_answer():
			with self.assertRaises(bolna.BolnaOutcomeUnknown):
				bolna.place_call({"api_key": "k", "base_url": "https://api.bolna.ai"}, _GOOD, "agent-1",
				                 None, _TOKEN)

	def test_the_unknown_does_not_inherit_the_service_error(self):
		"""Not because today's caller would break — `_deliver_voice` matches the unknown clause FIRST, so
		inheritance survives there by ordering alone. That is the reason to lock it: the next caller to
		handle only `BolnaServiceError`, or a reorder of those two clauses, would un-count and replay a
		dial that may already have reached the patient, and nothing would say so."""
		self.assertFalse(
			issubclass(bolna.BolnaOutcomeUnknown, bolna.BolnaServiceError),
			"an unknown outcome that IS a service error is not a separate outcome at all",
		)

	def test_a_refusal_still_raises(self):
		"""The other half. Bolna's declared 4xx is an ANSWER — it really did decline — so it keeps the
		documented behaviour: the cap slot comes back and the job fails where a human can see it."""
		refused = MagicMock(status_code=400, content=b"{}")
		refused.json.return_value = {"message": "bad agent"}
		with patch("tatva_connect.voice.adapters.bolna.requests.post", return_value=refused):
			with self.assertRaises(bolna.BolnaServiceError):
				bolna.place_call({"api_key": "k", "base_url": "https://api.bolna.ai"}, _GOOD, "agent-1",
				                 None, _TOKEN)

	def test_the_delivery_job_records_the_unknown_instead_of_failing(self):
		"""The outcome that matters: the job COMPLETES. A raise here is what files it where replay lives."""
		with self._no_answer():
			outcome = sends._deliver_voice(_ACCOUNT, _GOOD, "agent-1", None, "LEAD-1", correlation=_TOKEN)

		self.assertIsNone(outcome, "the unknown branch must return, not raise onto the failed registry")

	def test_nothing_is_written_for_a_call_we_cannot_confirm(self):
		"""No execution_id came back, so there is nothing to correlate and no call to log. Writing a row
		would claim a dial happened; the Wait's own timeout leg is what frees the journey either way."""
		before = frappe.db.count("CRM Call Log")

		with self._no_answer():
			sends._deliver_voice(_ACCOUNT, _GOOD, "agent-1", None, "LEAD-1", correlation=_TOKEN)

		self.assertEqual(frappe.db.count("CRM Call Log"), before,
		                 "a call we never confirmed was logged as if it had been placed")
