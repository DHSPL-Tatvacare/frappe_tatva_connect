"""Two workers, one delivery. The spine must not turn a provider's re-send into a failure.

A provider re-sends: one live Acefone CDR arrived ELEVEN times, byte for byte. Each copy is its own
Integration Request and its own queued job, and `queue-short` and `queue-long` BOTH drain the short
queue — so the copies write the same call at once. Measured on this bench before the fix: one row
written, NINE deliveries stamped Failed. The call data was right; the failure log was a lie, and the DLQ
replay re-fired the nine and re-failed them, so it could never drain.

Both halves of the answer are held here, and both live in the spine, so every adapter inherits them.
"""
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.webhooks import spine

PAYLOAD = {"call_id": "_spine-conc-1", "call_status": "missed", "duration": 292}


class TestDeliveryKey(unittest.TestCase):
	def test_identical_deliveries_share_one_key(self):
		"""The eleven copies were byte-identical, so they are one delivery and need run once."""
		a = spine._delivery_key("Acefone", "inbound_complete", dict(PAYLOAD))
		b = spine._delivery_key("Acefone", "inbound_complete", dict(reversed(list(PAYLOAD.items()))))
		self.assertEqual(a, b, "key depends on dict order")

	def test_a_delivery_that_says_something_new_gets_its_own_key(self):
		"""The hangup CDR carries the duration and the recording. It must never be deduplicated away."""
		answered = spine._delivery_key("Acefone", "inbound_answered", {**PAYLOAD, "duration": 0})
		hangup = spine._delivery_key("Acefone", "inbound_complete", PAYLOAD)
		self.assertNotEqual(answered, hangup)

	def test_the_key_is_scoped_to_its_provider(self):
		"""Two providers are free to send the same bytes."""
		self.assertNotEqual(
			spine._delivery_key("Acefone", "x", PAYLOAD), spine._delivery_key("WATI", "x", PAYLOAD)
		)


class TestSpineConcurrency(FrappeTestCase):
	def test_an_identical_delivery_already_in_flight_is_not_queued_twice(self):
		"""Frappe refuses the duplicate enqueue and returns None. The row must say so, not sit Queued."""
		with patch.object(spine.frappe, "enqueue", return_value=None) as enq, \
		     patch.object(spine, "_persist", return_value="_test-log-row"), \
		     patch.object(spine, "_screen", return_value=(True, None)), \
		     patch.object(spine, "_request_payload", return_value=dict(PAYLOAD)), \
		     patch.object(spine.ingress, "verify", return_value="_TestTelephonyAcct"), \
		     patch.object(spine, "_mark") as mark:
			spine.receive("Acefone", enabled=lambda: True, adapter=None, event="inbound_complete")

		self.assertTrue(enq.call_args.kwargs["deduplicate"], "the enqueue is not deduplicated")
		self.assertEqual(
			enq.call_args.kwargs["job_id"],
			spine._delivery_key("Acefone", "inbound_complete", PAYLOAD),
		)
		mark.assert_called_once()
		self.assertEqual(mark.call_args[0][1], "Cancelled")
		self.assertIn("already in flight", mark.call_args.kwargs["output"]["reason"])

	def test_a_write_that_loses_the_race_is_retried_by_frappe_not_failed(self):
		"""The loser is not a broken delivery. It raises Frappe's own retry signal, and execute_job
		re-runs the job; the next pass sees the winner's row and completes.

		Without this the DLQ replay is self-defeating: it fires one job per stored row, so replaying a
		call delivered eleven times fires eleven at once, they collide, and every one is marked Failed
		again."""
		adapter = _adapter(handle_raises=frappe.QueryDeadlockError("1020 snapshot conflict"))
		with patch.object(spine, "_adapter_for", return_value=adapter), \
		     patch.object(spine, "_mark"):
			with self.assertRaises(frappe.RetryBackgroundJobError):
				spine.process("Acefone", dict(PAYLOAD), "_TestTelephonyAcct",
				              vendor_event="inbound_complete", log="_test-log-row")

	def test_every_way_the_same_collision_surfaces_is_retried(self):
		"""One collision, four faces: the winner still in flight (deadlock / 1020 / lock timeout), or
		already committed and this one hit the primary key or a unique column."""
		for exc in (frappe.QueryDeadlockError, frappe.QueryTimeoutError,
		            frappe.DuplicateEntryError, frappe.UniqueValidationError):
			with self.subTest(exc=exc.__name__):
				adapter = _adapter(handle_raises=exc("collision"))
				with patch.object(spine, "_adapter_for", return_value=adapter), \
				     patch.object(spine, "_mark"):
					with self.assertRaises(frappe.RetryBackgroundJobError):
						spine.process("Acefone", dict(PAYLOAD), "_TestTelephonyAcct", log="_x")

	def test_a_genuinely_broken_delivery_still_fails_to_the_dlq(self):
		"""The retry must not swallow a real bug. Only a collision is re-run."""
		adapter = _adapter(handle_raises=ValueError("the adapter is broken"))
		with patch.object(spine, "_adapter_for", return_value=adapter), \
		     patch.object(spine, "_mark") as mark:
			with self.assertRaises(ValueError):
				spine.process("Acefone", dict(PAYLOAD), "_TestTelephonyAcct", log="_test-log-row")
		self.assertEqual(mark.call_args[0][1], "Failed")

	def test_a_delivery_whose_call_is_already_written_completes_without_writing(self):
		"""The second pass of a retried job. This is the gate that makes the retry terminate."""
		adapter = _adapter(already=True)
		with patch.object(spine, "_adapter_for", return_value=adapter), \
		     patch.object(spine, "_mark") as mark:
			spine.process("Acefone", dict(PAYLOAD), "_TestTelephonyAcct", log="_test-log-row")
		self.assertEqual(mark.call_args[0][1], "Completed")
		self.assertEqual(mark.call_args.kwargs["output"]["outcome"], "already processed")
		adapter.handle.assert_not_called()


def _adapter(handle_raises=None, already=False):
	from unittest.mock import MagicMock

	mod = MagicMock()
	mod.already_processed.return_value = already
	mod.handle.side_effect = handle_raises
	return mod
