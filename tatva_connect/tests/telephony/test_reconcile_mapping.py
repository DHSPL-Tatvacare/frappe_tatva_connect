"""The pull path maps a Call Detail Record exactly as the push path maps a webhook.

Both are pinned against a live Acefone record (account 214181). The two sources carry the same facts
differently — a webhook says "Dialer (inbound)", a record says `direction: inbound` plus
`call_hint: dialer` — and the whole point of this module is that they end up at the same envelope. A
drift here means the same call is logged one way when pushed and another when pulled.
"""
import unittest

from tatva_connect.telephony import reconcile
from tatva_connect.telephony.adapters import acefone

# One real record, trimmed. The webhook for this same call arrives as
# {"call_id": "...", "direction": "Dialer (outbound)", "caller_id_number": "+919240289226", ...}
RECORD = {
	"call_id": "12a445d3-635d-45da-a3b9-161ac269a3a0",
	"uuid": "6a5384e2b1c07",
	"direction": "outbound",
	"call_hint": "dialer",
	"status": "answered",
	"client_number": "+919678119748",
	"did_number": "+919240289226",
	"call_duration": 52,
	"date": "2026-07-12",
	"time": "17:34:58",
	"end_stamp": "2026-07-12 17:35:49",
	"hangup_cause": "disconnected_by_caller",
	"agent_name": "Shaik Khamrunisa Nisha",
	"agent_number": "Extension-0602141810277",
	"recording_url": "https://console.acefone.in/file/recording?callId=x&type=rec&token=y",
	"call_flow": [
		{"type": "init", "value": "1783857298"},
		{"type": "Agent", "id": "05021", "name": "Shaik", "num": "Extension-0602141810277",
		 "email": "s.kamrunissa@tatvacare.in", "dialst": "Dialed"},
		{"type": "Agent", "id": "05021", "name": "Shaik", "num": "Extension-0602141810277",
		 "email": "s.kamrunissa@tatvacare.in", "dialst": "Answered"},
	],
}


def _envelope(record):
	direction = reconcile._norm_direction(record)
	payload = reconcile._report_to_payload(record, direction)
	return acefone.normalize(payload)


class TestReconcileMapping(unittest.TestCase):
	def setUp(self):
		self.cdr = _envelope(RECORD)

	def test_the_call_key_is_the_same_one_the_webhook_sends(self):
		"""`call_id` was an open question for months — a record and a webhook use the same key.

		Confirmed against a live record whose Call Log the webhook had already created. Were it not
		true, reconcile would duplicate every call it touched.
		"""
		self.assertEqual(self.cdr["call_key"], RECORD["call_id"])

	def test_the_record_names_the_parties_and_they_land_the_right_way_round(self):
		"""A record is explicit where a webhook is not: `client_number` is always the customer and
		`did_number` always ours, whichever way the call went."""
		self.assertEqual(self.cdr["direction"], "outbound")
		self.assertEqual(self.cdr["customer_number"], "9678119748")
		self.assertEqual(self.cdr["did_number"], "9240289226")

	def test_the_channel_agrees_with_what_the_webhook_would_have_said(self):
		"""`call_hint: dialer` is the same fact the webhook spells "Dialer (outbound)".

		If this drifts, a channel-scoped capture rule keeps the call when it is pushed and drops it
		when it is pulled — the same call, two answers.
		"""
		self.assertEqual(self.cdr["channel"], "Dialer")

		ivr = _envelope({**RECORD, "call_hint": "inbound", "direction": "inbound"})
		self.assertEqual(ivr["channel"], "IVR")
		self.assertEqual(ivr["direction"], "inbound")

	def test_the_agent_resolves_from_the_call_flow(self):
		"""The record has no agent-email field, but its `call_flow` carries one — and the email is the
		only identifier that resolves to a CRM user. A reconciled call attributes its rep or the pull
		path would quietly log everything unattributed."""
		self.assertEqual(self.cdr["agent_key"], "s.kamrunissa@tatvacare.in")

	def test_status_duration_and_timestamps_survive_the_record_shape(self):
		"""A record splits the start into `date` + `time`; a webhook sends one stamp."""
		self.assertEqual(self.cdr["status"], "Completed")
		self.assertTrue(self.cdr["connected"])
		self.assertEqual(self.cdr["duration_sec"], 52)
		self.assertEqual(str(self.cdr["started_at"]), "2026-07-12 17:34:58")
		self.assertEqual(str(self.cdr["ended_at"]), "2026-07-12 17:35:49")

	def test_a_missed_call_is_not_marked_connected(self):
		missed = _envelope({**RECORD, "status": "missed"})
		self.assertEqual(missed["status"], "No Answer")
		self.assertFalse(missed["connected"])

	def test_the_response_envelope_is_results_not_data(self):
		"""Acefone answers {count, limit, size, page, results}. Reading the wrong key silently
		reconciles nothing at all."""
		self.assertEqual(reconcile._rows_from_report({"count": 1, "results": [RECORD]}), [RECORD])
		self.assertEqual(reconcile._rows_from_report({"count": 0, "results": []}), [])
