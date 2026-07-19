# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Tests for the shared inbound-webhook spine (tatva_connect/webhooks/spine.py).

The spine is the ONE front door every CHANNEL (whatsapp, telephony) flows through:
kill-switch (default OFF) -> token auth+scope (fail-closed) -> always-on raw log
(Integration Request) -> cheap pre-filter -> fast 'ok' ACK + enqueue; the worker
dedupes then hands to the adapter, exceptions falling to the RQ failed registry (the
DLQ) rather than being swallowed behind a 200.

The door is keyed by channel and takes NO adapter argument: the token resolves the
account and the account's own provider field names the adapter, so no endpoint carries
a vendor. These exercise the spine itself with the adapter resolution mocked, so the
contract is asserted independent of any provider's DB moves. The WhatsApp event shape
is a verified live one from docs/plans/2026-06-19-webhook-ingress-spine.md.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.channels import resolve
from tatva_connect.webhooks import spine

# --- WATI event shapes (verified live samples; see the plan) ---------------
_ACCOUNT = "WATI Test Account"
_TOKEN = "tok-secret-abc123"

# inbound customer message (eventType=message, owner=false) — idempotency key is whatsappMessageId
_WATI_INBOUND = {
	"eventType": "message",
	"owner": False,
	"whatsappMessageId": "wamid.TESTINBOUND001",
	"waId": "919812345678",
	"text": "hello from the patient",
	"type": "text",
	"conversationId": "conv-001",
	"senderName": "Test Patient",
	"id": "wati-internal-001",
	"timestamp": "1718784000",
}


def _fake_request(token=None):
	"""A minimal stand-in for frappe.request — only .args.get is read by _request_token."""
	args = {"token": token} if token is not None else {}
	return SimpleNamespace(args=args)


class TestWebhookSpine(FrappeTestCase):
	def setUp(self):
		# A relevant-by-default adapter; individual tests override its methods.
		self.adapter = MagicMock()
		self.adapter.screen.return_value = (True, None)
		self.adapter.already_processed.return_value = False
		# Front door reads the token off the (faked) request + form_dict payload.
		frappe.local.request = _fake_request(_TOKEN)
		frappe.form_dict.update({"cmd": "x", "token": _TOKEN, **_WATI_INBOUND})

	def tearDown(self):
		frappe.local.request = None
		frappe.form_dict.clear()
		frappe.set_user("Administrator")

	# --- (a) kill-switch OFF -> 'ok', nothing enqueued, nothing logged ----

	def test_killswitch_off_acks_and_does_no_work(self):
		"""Default-OFF dormancy: a disabled integration ACKs fast, screens nothing and enqueues nothing.

		It DOES still record what arrived — see test_the_kill_switch_records_what_it_declined_to_act_on.
		"""
		with patch.object(spine.ingress, "verify", return_value=_ACCOUNT), \
		     patch.object(spine, "_adapter_for", return_value=self.adapter), \
		     patch("frappe.enqueue") as enqueue:
			result = spine.receive("whatsapp", enabled=lambda: False)
		self.assertEqual(result, "ok")
		enqueue.assert_not_called()
		self.adapter.screen.assert_not_called()

	# --- (b) bad token -> PermissionError (fail-closed) -------------------

	def test_bad_token_raises_permission_error(self):
		"""An unresolvable token is rejected before any
		payload work — fail-closed, never 'ok'."""
		with patch.object(spine, "_persist") as persist, \
		     patch.object(spine.ingress, "verify", side_effect=frappe.PermissionError), \
		     patch("frappe.enqueue") as enqueue:
			with self.assertRaises(frappe.PermissionError):
				spine.receive("whatsapp", enabled=lambda: True)
		persist.assert_not_called()
		enqueue.assert_not_called()

	# --- (c) good token -> fast 'ok' + ONE raw log + ONE enqueue ----------

	def test_good_token_acks_logs_once_enqueues_once(self):
		"""Happy path: resolve -> persist exactly one Integration Request -> enqueue the
		worker exactly once -> return 'ok'. The raw log is real (asserted in the DB)."""
		before = frappe.db.count("Integration Request", {"integration_request_service": "whatsapp"})
		with patch("frappe.enqueue") as enqueue, \
		     patch.object(spine.ingress, "verify", return_value=_ACCOUNT), \
		     patch.object(spine, "_adapter_for", return_value=self.adapter):
			result = spine.receive("whatsapp", enabled=lambda: True)
		self.assertEqual(result, "ok")

		after = frappe.db.count("Integration Request", {"integration_request_service": "whatsapp"})
		self.assertEqual(after - before, 1, "exactly one raw Integration Request persisted")

		enqueue.assert_called_once()
		_args, kwargs = enqueue.call_args
		self.assertEqual(_args[0], "tatva_connect.webhooks.spine.process")
		# The raw log and the job are keyed by CHANNEL — a vendor's name reaches neither.
		self.assertEqual(kwargs["channel"], "whatsapp")
		self.assertEqual(kwargs["account"], _ACCOUNT)
		self.assertEqual(kwargs["payload"]["whatsappMessageId"], "wamid.TESTINBOUND001")
		# The persisted row name is handed to the worker (so it can flip status).
		self.assertTrue(kwargs["log"])
		# Regression guard: the vendor sub-event is forwarded as 'vendor_event', NEVER 'event' ('event' is a reserved frappe.enqueue kwarg — passing it there silently drops it).
		self.assertIn("vendor_event", kwargs)
		self.assertNotIn("event", kwargs)

	def test_vendor_event_forwarded_through_enqueue(self):
		"""Regression for the reserved-kwarg trap: an Acefone trigger carried in `event` must
		reach process() as `vendor_event`. If it were enqueued as `event=...` it would bind to
		frappe.enqueue's own arg and process() would run with vendor_event=None — mislabeling
		every Acefone call's direction/completion."""
		frappe.form_dict.clear()
		frappe.local.request = _fake_request(_TOKEN)
		frappe.form_dict.update({"token": _TOKEN, "call_id": "ACE-001"})
		with patch("frappe.enqueue") as enqueue, \
		     patch.object(spine.ingress, "verify", return_value="Acefone Test Account"), \
		     patch.object(spine, "_adapter_for", return_value=self.adapter):
			spine.receive("telephony", enabled=lambda: True, event="inbound_complete")
		_args, kwargs = enqueue.call_args
		self.assertEqual(kwargs["vendor_event"], "inbound_complete")
		self.assertNotIn("event", kwargs)

	def test_irrelevant_event_is_logged_as_cancelled_with_a_reason(self):
		"""A declined delivery is still logged, and the log says so.

		It used to be written as 'Queued' and left there for ever, which made a call dropped on
		purpose indistinguishable from one that was stuck. It is now 'Cancelled', with the reason on
		the row, and it stays replayable from the stored payload."""
		self.adapter.screen.return_value = (False, "DID 9240276221 is not mapped to a grain")
		before = frappe.db.count("Integration Request", {"integration_request_service": "whatsapp"})
		with patch("frappe.enqueue") as enqueue, \
		     patch.object(spine.ingress, "verify", return_value=_ACCOUNT), \
		     patch.object(spine, "_adapter_for", return_value=self.adapter):
			result = spine.receive("whatsapp", enabled=lambda: True)
		self.assertEqual(result, "ok")
		enqueue.assert_not_called()

		after = frappe.db.count("Integration Request", {"integration_request_service": "whatsapp"})
		self.assertEqual(after - before, 1, "a declined delivery is still logged")

		row = frappe.get_last_doc("Integration Request", filters={"integration_request_service": "whatsapp"})
		self.assertEqual(row.status, "Cancelled")
		self.assertIn("not mapped to a grain", row.output)

	# --- (d) adapter dedupe -> second identical event is a no-op ----------

	def test_dedupe_second_event_is_noop(self):
		"""The worker dedupes via the adapter: already_processed=True short-circuits BEFORE
		handle(), so a redelivered event does no DB moves."""
		with patch.object(spine, "_adapter_for", return_value=self.adapter):
			# First delivery: not seen yet -> handle runs.
			self.adapter.already_processed.return_value = False
			spine.process("whatsapp", _WATI_INBOUND, _ACCOUNT, vendor_event=None, log=None)
			self.assertEqual(self.adapter.handle.call_count, 1)

			# Second, identical delivery: now seen -> handle must NOT run again.
			self.adapter.already_processed.return_value = True
			spine.process("whatsapp", _WATI_INBOUND, _ACCOUNT, vendor_event=None, log=None)
			self.assertEqual(self.adapter.handle.call_count, 1, "redelivery must not re-handle")

	# --- (e) replay() re-runs process from a stored Integration Request ---

	def test_replay_reruns_process_from_stored_request(self):
		"""spine.replay loads a stored raw Integration Request and re-drives process():
		correct service, payload recovered from `data`, event parsed out of
		request_description, account re-derived via the adapter's account_for_payload."""
		log = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "whatsapp",
				"request_description": "whatsapp",  # WhatsApp carries no sub-event -> recovered as None
				"status": "Queued",
				"data": json.dumps(_WATI_INBOUND, default=str),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		with patch.object(spine.resolve, "adapter_for_payload", return_value=(self.adapter, _ACCOUNT)), \
		     patch.object(spine, "process") as proc:
			out = spine.replay(log.name)

		self.assertEqual(out, "ok")
		proc.assert_called_once()
		_args, kwargs = proc.call_args
		self.assertEqual(_args[0], "whatsapp")                   # channel
		self.assertEqual(_args[1]["whatsappMessageId"], "wamid.TESTINBOUND001")  # payload
		self.assertEqual(_args[2], _ACCOUNT)                     # account re-derived
		self.assertIsNone(kwargs["vendor_event"])                # no sub-event on this channel
		self.assertEqual(kwargs["log"], log.name)               # re-uses the same log row

	# --- (f) Acefone regression: handler exception propagates (DLQ) -------

	def test_handler_exception_propagates_to_dlq(self):
		"""The Acefone swallow-as-200 bug is fixed: a failure inside the adapter's handle()
		propagates OUT of process() (so it lands in the RQ failed registry / DLQ), instead
		of being caught and returned as 'ok'."""
		boom = self.adapter
		boom.already_processed.return_value = False
		boom.handle.side_effect = ValueError("Acefone CDR exploded")
		with patch.object(spine, "_adapter_for", return_value=boom):
			with self.assertRaises(ValueError):
				spine.process("telephony", {"call_id": "ACE-001"}, "Acefone Test Account",
				              vendor_event="inbound_complete", log=None)

	# --- (g) M1: a raising handle() flips the raw log to 'Failed' before re-raising

	def test_handler_exception_marks_log_failed(self):
		"""M1: when handle() raises, process() must mark its Integration Request 'Failed'
		(truthful DLQ row) and THEN re-raise — so replay_channel can later target it and the
		exception still reaches the RQ failed registry. The Queued->Failed flip is asserted
		on a real stored row."""
		log = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "telephony",
				"request_description": "telephony inbound_complete",
				"status": "Queued",
				"data": json.dumps({"call_id": "ACE-001"}, default=str),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		boom = self.adapter
		boom.already_processed.return_value = False
		boom.handle.side_effect = ValueError("Acefone CDR exploded")
		with patch.object(spine, "_adapter_for", return_value=boom):
			with self.assertRaises(ValueError):
				spine.process("telephony", {"call_id": "ACE-001"}, "Acefone Test Account",
				              vendor_event="inbound_complete", log=log.name)

		status, error = frappe.db.get_value("Integration Request", log.name, ["status", "error"])
		self.assertEqual(status, "Failed", "a raised handle() must leave a truthful 'Failed' DLQ row")
		self.assertIn("Acefone CDR exploded", error or "", "the traceback belongs on the row, not only in Redis")

	# --- (i) a replay that does nothing must not claim it did -------------

	def test_a_replay_that_still_declines_stays_cancelled(self):
		"""A replay that changed nothing must not report success.

		It used to mark the row Completed regardless — so the act of trying to recover a dropped call
		quietly destroyed the operator's list of dropped calls, with no way back.
		"""
		log = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "whatsapp",
				"request_description": "whatsapp",
				"status": "Cancelled",
				"data": json.dumps(_WATI_INBOUND, default=str),
				"output": json.dumps({"outcome": "not captured", "reason": "an old reason"}),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		self.adapter.screen.return_value = (False, "still not mapped")
		with patch.object(spine.resolve, "adapter_for_payload", return_value=(self.adapter, _ACCOUNT)), \
		     patch.object(spine, "process") as proc:
			spine.replay(log.name)

		proc.assert_not_called()
		status, output = frappe.db.get_value("Integration Request", log.name, ["status", "output"])
		self.assertEqual(status, "Cancelled", "a declined replay must not become Completed")
		self.assertIn("still not mapped", output, "and its reason must be refreshed")

	def test_a_replay_that_now_qualifies_is_processed(self):
		"""The other half: once the configuration is fixed, the same row goes through."""
		log = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "whatsapp",
				"request_description": "whatsapp",
				"status": "Cancelled",
				"data": json.dumps(_WATI_INBOUND, default=str),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		self.adapter.screen.return_value = (True, None)
		with patch.object(spine.resolve, "adapter_for_payload", return_value=(self.adapter, _ACCOUNT)), \
		     patch.object(spine, "process") as proc:
			spine.replay(log.name)

		proc.assert_called_once()

	# --- (j) a dormant integration keeps an audit trail -------------------

	def test_the_kill_switch_records_what_it_declined_to_act_on(self):
		"""A switch that is off means "do not act on this", not "destroy it".

		Providers retry very few times, so a delivery dropped while the switch was off would be gone for
		good. Logged Cancelled, it is replayable the moment the integration is turned on.
		"""
		before = frappe.db.count("Integration Request", {"integration_request_service": "whatsapp"})
		with patch.object(spine.ingress, "verify", return_value=_ACCOUNT), \
		     patch.object(spine, "_adapter_for", return_value=self.adapter), \
		     patch("frappe.enqueue") as enqueue:
			result = spine.receive("whatsapp", enabled=lambda: False)

		self.assertEqual(result, "ok")
		enqueue.assert_not_called()
		self.adapter.screen.assert_not_called()

		after = frappe.db.count("Integration Request", {"integration_request_service": "whatsapp"})
		self.assertEqual(after - before, 1, "a dormant integration still records what arrived")

		row = frappe.get_last_doc("Integration Request", filters={"integration_request_service": "whatsapp"})
		self.assertEqual(row.status, "Cancelled")
		self.assertIn("switched off", row.output)

	# --- (h) M2: replay_channel re-enqueues ONLY the requested status -----

	def test_replay_channel_targets_only_the_requested_status(self):
		"""M2: replay_channel must re-enqueue exactly the Failed rows for a channel and skip
		Queued/Completed ones — a still-in-flight (Queued) row is never replayed. Asserted via
		the names actually handed to enqueue (one per matching row)."""
		svc = "ReplayScopeSvc"
		for stale in frappe.get_all("Integration Request", filters={"integration_request_service": svc}, pluck="name"):
			frappe.delete_doc("Integration Request", stale, force=True, ignore_permissions=True)
		failed = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": svc,
				"status": "Failed",
				"data": "{}",
			}
		).insert(ignore_permissions=True)
		# A Queued (in-flight) and a Completed row for the same service must be ignored.
		frappe.get_doc(
			{"doctype": "Integration Request", "integration_request_service": svc,
			 "status": "Queued", "data": "{}"}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{"doctype": "Integration Request", "integration_request_service": svc,
			 "status": "Completed", "data": "{}"}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		with patch("frappe.enqueue") as enqueue:
			count = spine.replay_channel(svc)

		self.assertEqual(count, 1, "only the single Failed row is in scope")
		self.assertEqual(enqueue.call_count, 1)
		_args, kwargs = enqueue.call_args
		self.assertEqual(_args[0], "tatva_connect.webhooks.spine.replay")
		self.assertEqual(kwargs["integration_request"], failed.name)


class TestReplayIdentifiesItsOwnVendor(FrappeTestCase):
	"""A replayed delivery carries no token, and the token is what names the vendor on a live one.

	The account is therefore re-derived from the PAYLOAD: every adapter is offered it and the one that
	recognises it as its own answers. Before this, replay used the channel's only installed adapter,
	which is right until a second vendor exists and silently wrong the moment one does.
	"""

	def _channel_of(self, **adapters):
		"""Patch a channel to carry exactly these providers, `frappe.get_module` handing back doubles."""
		cfg = {"channel": "whatsapp", "account_doctype": "WhatsApp Account",
		       "adapters": {name: f"double.{name}" for name in adapters}}
		mods = {f"double.{name}": mod for name, mod in adapters.items()}
		return (patch.object(resolve.registry, "by_channel", return_value=cfg),
		        patch.object(frappe, "get_module", side_effect=lambda path: mods[path]))

	@staticmethod
	def _double(claims=None, raises=False):
		mod = MagicMock()
		if raises:
			mod.account_for_payload.side_effect = RuntimeError("this payload is not mine to read")
		else:
			mod.account_for_payload.return_value = claims
		return mod

	def test_the_adapter_that_claims_the_payload_owns_it(self):
		"""Not the first registered, and not the only one — the one that recognises the payload."""
		stranger, owner = self._double(claims=None), self._double(claims="Owner Account")
		by_channel, get_module = self._channel_of(STRANGER=stranger, OWNER=owner)
		with by_channel, get_module:
			adapter, account = resolve.adapter_for_payload("whatsapp", _WATI_INBOUND)

		self.assertIs(adapter, owner, "the claiming adapter must own the delivery")
		self.assertEqual(account, "Owner Account")
		stranger.account_for_payload.assert_called_once()

	def test_a_payload_no_provider_claims_resolves_to_nothing(self):
		"""Declining to replay beats running one vendor's parser over another's payload."""
		a, b = self._double(claims=None), self._double(claims=None)
		by_channel, get_module = self._channel_of(A=a, B=b)
		with by_channel, get_module:
			adapter, account = resolve.adapter_for_payload("whatsapp", _WATI_INBOUND)

		self.assertIsNone(adapter, "an unclaimed payload must not be handed to a guessed adapter")
		self.assertIsNone(account)

	def test_a_raising_resolver_does_not_deny_the_true_owner(self):
		"""A foreign payload can legitimately break another vendor's resolver; logged, then moved past."""
		broken, owner = self._double(raises=True), self._double(claims="Owner Account")
		by_channel, get_module = self._channel_of(BROKEN=broken, OWNER=owner)
		with by_channel, get_module, patch.object(frappe, "log_error") as logged:
			adapter, account = resolve.adapter_for_payload("whatsapp", _WATI_INBOUND)

		self.assertIs(adapter, owner)
		self.assertEqual(account, "Owner Account")
		logged.assert_called_once()

	def test_replay_refuses_a_delivery_no_provider_recognises(self):
		"""End to end: an unidentifiable stored row is refused, never replayed against a guess."""
		log = frappe.get_doc(
			{"doctype": "Integration Request", "integration_request_service": "whatsapp",
			 "request_description": "whatsapp", "status": "Failed",
			 "data": json.dumps(_WATI_INBOUND, default=str)}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		with patch.object(spine.resolve, "adapter_for_payload", return_value=(None, None)), \
		     patch.object(spine, "process") as proc:
			with self.assertRaises(frappe.ValidationError):
				spine.replay(log.name)

		proc.assert_not_called()
