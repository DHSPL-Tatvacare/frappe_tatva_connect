"""Two writers, one delivery. The spine must not turn a provider's re-send into a failure.

A provider re-sends: one live Acefone CDR arrived ELEVEN times, byte for byte. Measured on this bench before the
spine handled it: one row written, NINE deliveries stamped Failed. The stored rows are now worked in arrival order,
and a write that still loses a race to another writer of the same call runs again in place until it sees the winner.
"""
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.webhooks import spine

PAYLOAD = {"call_id": "_spine-conc-1", "call_status": "missed", "duration": 292}
COLLISIONS = (frappe.QueryDeadlockError, frappe.QueryTimeoutError, frappe.DuplicateEntryError, frappe.UniqueValidationError)


class TestSpineConcurrency(FrappeTestCase):
	def _process(self, adapter):
		with patch.object(spine, "_adapter_for", return_value=adapter), patch.object(spine, "_mark") as mark, \
		     patch.object(spine.time, "sleep"):
			spine.process("telephony", dict(PAYLOAD), "_TestTelephonyAcct", vendor_event="inbound_complete", log="_test-log-row")
		return mark

	def test_every_way_the_same_collision_surfaces_runs_again_in_place_and_completes(self):
		"""One collision, four faces: the winner still in flight (deadlock / 1020 / lock timeout), or already committed and
		this one hit the primary key or a unique column. None reaches Frappe's `RetryBackgroundJobError`."""
		for exc in COLLISIONS:
			with self.subTest(exc=exc.__name__):
				adapter = _adapter(handle_raises=[exc("collision"), None])
				mark = self._process(adapter)
				self.assertEqual(adapter.handle.call_count, 2)
				self.assertEqual(mark.call_args[0][1], "Completed")

	def test_the_second_pass_sees_the_winner_and_completes_without_writing(self):
		"""The run after a lost race asks `already_processed` first — the gate that makes the retry terminate."""
		adapter = _adapter(handle_raises=[frappe.QueryDeadlockError("1020 snapshot conflict")])
		adapter.already_processed.side_effect = [False, True]
		mark = self._process(adapter)
		self.assertEqual(adapter.handle.call_count, 1)
		self.assertEqual(mark.call_args.kwargs["output"]["outcome"], "already processed")

	def test_a_race_that_never_clears_fails_after_frappes_own_budget(self):
		adapter = _adapter(handle_raises=[frappe.QueryDeadlockError("held")] * spine.MAX_RETRIES)
		with self.assertRaises(frappe.QueryDeadlockError):
			self._process(adapter)
		self.assertEqual(adapter.handle.call_count, spine.MAX_RETRIES)

	def test_a_genuinely_broken_delivery_still_fails_to_the_dlq(self):
		"""The retry must not swallow a real bug. Only a collision is re-run."""
		adapter = _adapter(handle_raises=[ValueError("the adapter is broken")])
		with patch.object(spine, "_adapter_for", return_value=adapter), patch.object(spine, "_mark") as mark:
			with self.assertRaises(ValueError):
				spine.process("telephony", dict(PAYLOAD), "_TestTelephonyAcct", log="_test-log-row")
		self.assertEqual(adapter.handle.call_count, 1)
		self.assertEqual(mark.call_args[0][1], "Failed")

	def test_a_delivery_whose_call_is_already_written_completes_without_writing(self):
		adapter = _adapter(already=True)
		mark = self._process(adapter)
		self.assertEqual(mark.call_args[0][1], "Completed")
		self.assertEqual(mark.call_args.kwargs["output"]["outcome"], "already processed")
		adapter.handle.assert_not_called()


def _adapter(handle_raises=None, already=False):
	mod = MagicMock()
	mod.already_processed.return_value = already
	mod.handle.side_effect = handle_raises
	return mod
