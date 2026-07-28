# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""AN AI VOICE CALL SHOWS UP ON THE LEAD, in the table every other call already lands in.

Before this, a call happened to a patient and the CRM showed NOTHING — the only trace was an Integration
Request row from the webhook spine and an execution_id in a logger. A rep opening the lead saw no call.

THE ROW IS A `CRM Call Log`, written by the ONE writer (`telephony.bridge._new_call_log`) and marked
`telephony_medium = AI Voice` so it is distinguishable in a MIXED table (`telephony/reconcile.py:10`) —
provider rows, rep-typed Manual rows and now automation's rows all sit together, and nothing may disturb
telephony's reconciler or its permission conditions.

TWO WRITES, AND NO THIRD:
  * AT PLACEMENT, inside the DEFERRED `_deliver_voice` job — the same job that places the call, never a
    second enqueue, and never in the run's own segment. The row is bonded to the lead at birth.
  * AT THE TERMINAL WEBHOOK, the SAME row, found by `id`. `CRM Call Log.id` is UNIQUE and the doctype
    autonames from it, so the execution_id IS the row's name: idempotency is a primary-key seek, no new
    index, no correlation column invented.

IDEMPOTENCY IS THE POINT. One live Acefone CDR arrived ELEVEN times, byte for byte. The same webhook
delivered twice must leave ONE row and must not churn it — proven below by asserting `modified` is
untouched on the second delivery.

Nothing here dials. `requests` is never reached: placement is driven with `place_call` mocked, and the
webhook path is driven directly.
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import sends
from tatva_connect.voice.adapters import bolna
from tatva_connect.workflow_engine.tests import fixtures as fx

_ACCOUNT = "voice-calllog-account"
_EXEC = "exec-calllog-0001"
_NUMBER = "+919999999999"


def _callback(execution_id=_EXEC, status="completed", reason="", correlation=""):
	return {
		"id": execution_id,
		"status": status,
		"status_reason": reason,
		"user_data": {bolna.USER_DATA_CORRELATION_KEY: correlation},
		"transcript": "assistant: Hello.",
		"telephony_data": {
			"duration": 42, "recording_url": "https://example.invalid/rec.mp3",
			"to_number": _NUMBER, "from_number": "+918035303509",
		},
	}


class _CallLogCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.exists("CRM AI Voice Account", _ACCOUNT):
			frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "CRM AI Voice Account", "account_name": _ACCOUNT,
			"api_key": "sk-never-real", "from_phone": "+918035303509", "enabled": 1,  # pragma: allowlist secret
		}).insert(ignore_permissions=True)
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Call Log", {"telephony_medium": bolna.CALL_MEDIUM})
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM AI Voice Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.db.delete("CRM Call Log", {"telephony_medium": bolna.CALL_MEDIUM})
		frappe.db.commit()

	def _place(self, execution_id=_EXEC):
		"""Run the deferred delivery with the provider mocked — the same job the thunk enqueues."""
		placed = {"correlation_id": execution_id, "contact": _NUMBER, "mode": "single",
		          "from_phone": "+918035303509", "raw": {}}
		with patch("tatva_connect.voice.adapters.bolna.place_call", return_value=placed) as call:
			sends._deliver_voice(_ACCOUNT, _NUMBER, "agent-1", None, self.lead.name,
			                     correlation="run-x::voice-1")
		call.assert_called_once()
		return execution_id

	def _row(self, execution_id=_EXEC):
		return frappe.db.get_value("CRM Call Log", execution_id, "*", as_dict=True)


class TestThePlacementWritesTheCallOntoTheLead(_CallLogCase):
	"""Write one. The row exists the moment the provider accepts the call, bonded to the lead."""

	def test_a_placed_call_becomes_a_call_log_row(self):
		self._place()
		row = self._row()
		self.assertIsNotNone(row, "a call was placed to a patient and the CRM shows nothing")
		self.assertEqual(row.telephony_medium, bolna.CALL_MEDIUM)
		self.assertEqual(row.status, "Initiated")
		self.assertEqual(row.type, "Outgoing")

	def test_the_row_is_bonded_to_the_lead_at_birth(self):
		self._place()
		row = self._row()
		self.assertEqual(row.reference_doctype, "CRM Lead")
		self.assertEqual(row.reference_docname, self.lead.name)

	def test_the_execution_id_is_the_rows_own_name(self):
		"""`CRM Call Log.id` is UNIQUE and the doctype autonames from it, so the webhook's lookup is a
		primary-key seek. That is why no correlation column and no new index are needed."""
		self._place()
		self.assertEqual(self._row().id, _EXEC)
		self.assertTrue(frappe.db.exists("CRM Call Log", _EXEC))

	def test_it_carries_the_numbers_it_dialled(self):
		self._place()
		row = self._row()
		self.assertEqual(row.to, _NUMBER)
		self.assertEqual(getattr(row, "from"), "+918035303509")

	def test_it_claims_no_telephony_provider_key(self):
		"""A MIXED table. Telephony finds its rows by `custom_provider_call_id`; a voice row must not
		answer to that lookup or an Acefone CDR could land on top of an AI call."""
		self._place()
		from tatva_connect.telephony import writer

		self.assertFalse(self._row().get(writer.CALL_KEY_FIELD))

	def test_placement_costs_no_second_enqueue(self):
		"""The provider call and the log write are ONE background job. A second enqueue would mean the
		row and the call could disagree about whether the call happened."""
		with patch("frappe.enqueue") as enqueue:
			self._place()
		enqueue.assert_not_called()


class TestTheWebhookClosesTheSameRow(_CallLogCase):
	"""Write two. The terminal callback finds the row by its own name and finishes it."""

	def _deliver(self, **kwargs):
		bolna.update_call_log(_callback(**kwargs))

	def test_a_terminal_callback_completes_the_row(self):
		self._place()
		self._deliver()
		row = self._row()
		self.assertEqual(row.status, "Completed")
		self.assertEqual(row.duration, 42)
		self.assertIsNotNone(row.end_time)

	def test_the_row_points_at_our_file_or_at_nothing_at_all(self):
		"""STORED-OR-NOTHING. The producer's URL is public and temporary, so it never reaches this column:
		either the bytes became ours and this is our proxy URL, or there is no URL to play. Here the fetch
		is left to fail against an unreachable host, which is the honest 'nothing' case."""
		self._place()
		self._deliver()
		stored = self._row().recording_url
		self.assertIsNone(stored, "a recording we do not hold must not leave a playable URL on the call")

		with patch("tatva_connect.storage.call_media.store_recording", return_value="/api/method/ours?file_name=k"):
			bolna.update_call_log(_callback(status="completed"))
		self.assertEqual(self._row().recording_url, "/api/method/ours?file_name=k")

	def test_no_answer_lands_the_matching_status(self):
		self._place()
		self._deliver(status="no-answer")
		self.assertEqual(self._row().status, "No Answer")

	def test_a_failed_call_lands_failed(self):
		self._place()
		self._deliver(status="failed")
		self.assertEqual(self._row().status, "Failed")

	def test_call_disconnected_does_not_close_the_row(self):
		"""SETTLED (W7.4, vendor docs + 4 live calls): only `completed` is terminal. Treating the audio
		ending as the call ending closed the row early with the wrong outcome."""
		self._place()
		self._deliver(status="call-disconnected")
		self.assertEqual(self._row().status, "Initiated", "call-disconnected is not the end of a call")

	def test_a_callback_for_a_call_we_never_placed_writes_nothing(self):
		self._deliver(execution_id="exec-never-placed")
		self.assertFalse(frappe.db.exists("CRM Call Log", "exec-never-placed"),
		                 "the webhook must never invent a call row we have no placement for")


class TestARepeatedWebhookIsACheapNoOp(_CallLogCase):
	"""One live Acefone CDR arrived ELEVEN times, byte for byte. The same delivery twice must leave one
	row AND must not rewrite it — a read-modify-write on every copy churns the row and its timeline."""

	def test_the_same_callback_twice_leaves_one_row(self):
		self._place()
		bolna.update_call_log(_callback())
		bolna.update_call_log(_callback())
		self.assertEqual(
			frappe.db.count("CRM Call Log", {"telephony_medium": bolna.CALL_MEDIUM}), 1,
		)

	def test_the_second_delivery_does_not_touch_the_row(self):
		self._place()
		bolna.update_call_log(_callback())
		frappe.db.commit()
		before = frappe.db.get_value("CRM Call Log", _EXEC, "modified")
		with patch("frappe.db.set_value", wraps=frappe.db.set_value) as write:
			bolna.update_call_log(_callback())
		# Narrowed to THIS table on purpose: the media row is a different row with its own idempotency,
		# proven in tests/storage/test_call_media. What must not happen is a second write to the call.
		self.assertFalse([c for c in write.call_args_list if c.args and c.args[0] == "CRM Call Log"])
		self.assertEqual(frappe.db.get_value("CRM Call Log", _EXEC, "modified"), before)


class TestOneMapOwnsWhatAnOutcomeMeans(_CallLogCase):
	"""`bolna` already owns outcome meaning (`classify_outcome`). The call-log status is one more thing
	an outcome means, so the map lives beside it — never a second dialect at each call site."""

	def test_every_canonical_outcome_has_a_status(self):
		for outcome in ("answered", "no_answer", "failed"):
			self.assertIn(outcome, bolna.CALL_STATUS, f"{outcome} has no CRM Call Log status")

	def test_the_statuses_are_ones_the_doctype_declares(self):
		declared = set((frappe.get_meta("CRM Call Log").get_field("status").options or "").split("\n"))
		for outcome, status in bolna.CALL_STATUS.items():
			self.assertIn(status, declared, f"{outcome} maps to {status!r}, which the doctype does not offer")

	def test_the_medium_is_an_option_the_doctype_offers(self):
		"""Appended by a code Property Setter, because stock options drift between crm versions."""
		declared = (frappe.get_meta("CRM Call Log").get_field("telephony_medium").options or "").split("\n")
		self.assertIn(bolna.CALL_MEDIUM, [o.strip() for o in declared])
