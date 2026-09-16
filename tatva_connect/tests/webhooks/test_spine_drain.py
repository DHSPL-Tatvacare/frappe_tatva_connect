# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The stored rows are the queue: one drain works them oldest first, however large the burst, and never loses one."""
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.channels import retry
from tatva_connect.storage import call_media
from tatva_connect.tests.whatsapp.test_adapter_characterisation import _account
from tatva_connect.voice import reconcile
from tatva_connect.webhooks import spine
from tatva_connect.workflow_engine import thresholds

_ACCOUNT = "_Spine Drain Acct"
_TAG = "drain-test"


def _wanted():
	adapter = MagicMock()
	adapter.screen.return_value = (True, None)
	return adapter


def _stored(i, account=_ACCOUNT, status="Queued"):
	"""A delivery the door stored, through the document API, naming the account it arrived on."""
	name = frappe.get_doc({
		"doctype": "Integration Request", "integration_request_service": "whatsapp", "request_description": "whatsapp",
		"status": status, "data": frappe.as_json({"eventType": "message", "id": f"{_TAG}-{i}"}), "output": "",
		"reference_doctype": "WhatsApp Account" if account else None, "reference_docname": account,
	}).insert(ignore_permissions=True).name
	frappe.db.commit()
	return name


def _mine(limit, after=None):
	"""The drain's own query, narrowed to this test's rows so a shared bench's other Queued rows are never worked."""
	return [row for row in _real_queued(100000, after) if _TAG in (frappe.db.get_value("Integration Request", row.name, "data") or "")][:limit]


_real_queued = spine._queued


class _Case(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_account(_ACCOUNT, "919000000303")

	def setUp(self):
		frappe.db.delete("Integration Request", {"data": ["like", f"%{_TAG}-%"]})
		frappe.db.commit()

	tearDown = setUp


class TestKick(FrappeTestCase):
	def test_books_the_drain_on_the_short_lane(self):
		with patch.object(retry, "book", return_value="job") as book:
			spine.kick()
		book.assert_called_once_with(spine.drain, "webhook-drain", queue="short")

	def test_a_running_drain_gets_one_successor_and_never_a_third(self):
		with patch.object(retry, "book", side_effect=[None, "job"]) as book:
			spine.kick()
		self.assertEqual([call.args[1] for call in book.call_args_list], list(spine.DRAIN_JOBS))
		with patch.object(retry, "book", return_value=None) as book:
			self.assertIsNone(spine.kick())
		self.assertEqual(book.call_count, 2)


class TestTheDrain(_Case):
	def _drain(self, **patches):
		with patch.object(spine, "_queued", side_effect=_mine), patch.object(spine, "kick") as kick, \
		     patch.object(spine, "_adapter_for", return_value=patches.get("adapter", _wanted())), \
		     patch.object(spine, "process", **patches.get("process", {})) as proc:
			spine.drain()
		return proc, kick

	def test_works_every_stored_row_oldest_first_on_the_account_it_arrived_on(self):
		names = [_stored(i) for i in range(thresholds.WEBHOOK_DRAIN_BATCH + 3)]
		with patch.object(spine.resolve, "adapter_for_payload") as from_payload:
			proc, kick = self._drain()
		self.assertEqual([call.kwargs["log"] for call in proc.call_args_list], names)
		self.assertEqual({call.args[2] for call in proc.call_args_list}, {_ACCOUNT})
		from_payload.assert_not_called()
		kick.assert_not_called()

	def test_a_row_left_queued_is_visited_once_per_pass_never_spun_on(self):
		name = _stored(0)
		proc, kick = self._drain()
		self.assertEqual([call.kwargs["log"] for call in proc.call_args_list], [name])
		kick.assert_not_called()

	def test_a_spent_slice_hands_on_to_a_successor(self):
		_stored(0)
		with patch.object(thresholds, "WEBHOOK_DRAIN_SECONDS", 0):
			proc, kick = self._drain()
		proc.assert_not_called()
		kick.assert_called_once_with()

	def test_a_row_another_drain_already_finished_is_skipped(self):
		name = _stored(0, status="Completed")
		with patch.object(spine, "_adapter_for", return_value=_wanted()), patch.object(spine, "process") as proc:
			spine._work(name)
		proc.assert_not_called()

	def test_a_row_another_drain_has_claimed_is_skipped_not_waited_on(self):
		name = _stored(0)
		with patch.object(spine.frappe.db, "get_value", wraps=frappe.db.get_value) as claim, patch.object(spine, "process"):
			spine._work(name)
		self.assertTrue(claim.call_args.kwargs["for_update"] and claim.call_args.kwargs["skip_locked"])

	def test_a_failing_row_is_recorded_and_the_drain_moves_on(self):
		first, second = _stored(0), _stored(1)
		proc, _kick = self._drain(process={"side_effect": [ValueError("broken handler"), None]})
		self.assertEqual(proc.call_count, 2)
		status, error = frappe.db.get_value("Integration Request", first, ["status", "error"])
		self.assertEqual(status, "Failed")
		self.assertIn("broken handler", error)
		self.assertEqual(proc.call_args.kwargs["log"], second)

	def test_a_row_no_provider_recognises_is_failed_not_retried_for_ever(self):
		name = _stored(0, account=None)
		with patch.object(spine.resolve, "adapter_for_payload", return_value=(None, None)):
			proc, _kick = self._drain()
		proc.assert_not_called()
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Failed")

	def test_a_declined_row_is_cancelled_with_its_reason(self):
		name = _stored(0)
		declining = MagicMock()
		declining.screen.return_value = (False, "no CRM lead holds the number")
		proc, _kick = self._drain(adapter=declining)
		proc.assert_not_called()
		status, output = frappe.db.get_value("Integration Request", name, ["status", "output"])
		self.assertEqual(status, "Cancelled")
		self.assertIn("no CRM lead holds the number", output)


class TestOneWayToBookAPass(FrappeTestCase):
	def test_the_helper_books_one_deduplicated_pass(self):
		with patch("frappe.enqueue") as enqueue:
			retry.book(spine.drain, "a-pass", queue="short")
		enqueue.assert_called_once_with(spine.drain, queue="short", job_id="a-pass", deduplicate=True, now=True)

	def test_the_owed_work_sweeps_book_through_it(self):
		sweeps = (
			(call_media, patch.object(call_media.settings, "is_enabled", return_value=True), "call-media-sweep"),
			(reconcile, patch.object(reconcile.channel, "reconciler_enabled", return_value=True), "voice-reconcile-sweep"),
		)
		for module, armed, job_id in sweeps:
			with self.subTest(module=module.__name__), armed, patch.object(retry, "book") as book:
				module.sweep()
				book.assert_called_once_with(module._sweep, job_id)
