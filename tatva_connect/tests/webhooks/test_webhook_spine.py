# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Tests for the shared inbound-webhook spine (tatva_connect/webhooks/spine.py).

The spine is the ONE front door every vendor (WATI, Acefone) flows through:
kill-switch (default OFF) -> token auth+scope (fail-closed) -> always-on raw log
(Integration Request) -> cheap pre-filter -> fast 'ok' ACK + enqueue; the worker
dedupes then hands to the per-vendor adapter, exceptions falling to the RQ failed
registry (the DLQ) rather than being swallowed behind a 200.

These exercise the spine itself with the vendor callbacks/adapter mocked, so the
contract is asserted independent of WATI/Acefone DB moves. WATI event shapes are the
verified live ones from docs/plans/2026-06-19-webhook-ingress-spine.md.
"""
import json
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import routing
from tatva_connect import utils
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
		self.adapter.is_relevant.return_value = True
		self.adapter.already_processed.return_value = False
		# Front door reads the token off the (faked) request + form_dict payload.
		frappe.local.request = _fake_request(_TOKEN)
		frappe.form_dict.update({"cmd": "x", "token": _TOKEN, **_WATI_INBOUND})

	def tearDown(self):
		frappe.local.request = None
		frappe.form_dict.clear()
		frappe.set_user("Administrator")

	# --- (a) kill-switch OFF -> 'ok', nothing enqueued, nothing logged ----

	def test_killswitch_off_is_ok_noop(self):
		"""Default-OFF dormancy: a disabled integration returns 'ok' fast and does ZERO
		work — no account resolve, no raw log, no enqueue."""
		resolve = MagicMock()
		with patch.object(spine, "_persist_raw") as persist, \
		     patch("frappe.enqueue") as enqueue:
			result = spine.receive(
				"WATI",
				enabled=lambda: False,
				resolve_account=resolve,
				adapter=self.adapter,
			)
		self.assertEqual(result, "ok")
		resolve.assert_not_called()
		persist.assert_not_called()
		enqueue.assert_not_called()
		self.adapter.is_relevant.assert_not_called()

	# --- (b) bad token -> PermissionError (fail-closed) -------------------

	def test_bad_token_raises_permission_error(self):
		"""An unresolvable token (resolve_account returns None) is rejected before any
		payload work — fail-closed, never 'ok'."""
		with patch.object(spine, "_persist_raw") as persist, \
		     patch("frappe.enqueue") as enqueue:
			with self.assertRaises(frappe.PermissionError):
				spine.receive(
					"WATI",
					enabled=lambda: True,
					resolve_account=lambda token: None,
					adapter=self.adapter,
				)
		persist.assert_not_called()
		enqueue.assert_not_called()

	# --- (c) good token -> fast 'ok' + ONE raw log + ONE enqueue ----------

	def test_good_token_acks_logs_once_enqueues_once(self):
		"""Happy path: resolve -> persist exactly one Integration Request -> enqueue the
		worker exactly once -> return 'ok'. The raw log is real (asserted in the DB)."""
		before = frappe.db.count("Integration Request", {"integration_request_service": "WATI"})
		with patch("frappe.enqueue") as enqueue:
			result = spine.receive(
				"WATI",
				enabled=lambda: True,
				resolve_account=lambda token: _ACCOUNT if token == _TOKEN else None,
				adapter=self.adapter,
			)
		self.assertEqual(result, "ok")

		after = frappe.db.count("Integration Request", {"integration_request_service": "WATI"})
		self.assertEqual(after - before, 1, "exactly one raw Integration Request persisted")

		enqueue.assert_called_once()
		_args, kwargs = enqueue.call_args
		self.assertEqual(_args[0], "tatva_connect.webhooks.spine.process")
		self.assertEqual(kwargs["service"], "WATI")
		self.assertEqual(kwargs["account"], _ACCOUNT)
		self.assertEqual(kwargs["payload"]["whatsappMessageId"], "wamid.TESTINBOUND001")
		# The persisted row name is handed to the worker (so it can flip status).
		self.assertTrue(kwargs["log"])
		# Regression guard: the vendor sub-event is forwarded as 'vendor_event', NEVER 'event'
		# ('event' is a reserved frappe.enqueue kwarg — passing it there silently drops it).
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
		with patch("frappe.enqueue") as enqueue:
			spine.receive(
				"Acefone",
				enabled=lambda: True,
				resolve_account=lambda token: "Acefone Test Account",
				adapter=self.adapter,
				event="inbound_complete",
			)
		_args, kwargs = enqueue.call_args
		self.assertEqual(kwargs["vendor_event"], "inbound_complete")
		self.assertNotIn("event", kwargs)

	def test_irrelevant_event_acks_without_enqueue(self):
		"""The cheap pre-filter (adapter.is_relevant=False) still raw-logs but skips the
		enqueue — an optimistic drop that stays replayable from the stored payload."""
		self.adapter.is_relevant.return_value = False
		before = frappe.db.count("Integration Request", {"integration_request_service": "WATI"})
		with patch("frappe.enqueue") as enqueue:
			result = spine.receive(
				"WATI",
				enabled=lambda: True,
				resolve_account=lambda token: _ACCOUNT,
				adapter=self.adapter,
			)
		self.assertEqual(result, "ok")
		# Still logged (replayable) ...
		after = frappe.db.count("Integration Request", {"integration_request_service": "WATI"})
		self.assertEqual(after - before, 1)
		# ... but nothing enqueued.
		enqueue.assert_not_called()

	# --- (d) adapter dedupe -> second identical event is a no-op ----------

	def test_dedupe_second_event_is_noop(self):
		"""The worker dedupes via the adapter: already_processed=True short-circuits BEFORE
		handle(), so a redelivered event does no DB moves."""
		with patch.object(spine, "_adapter_for", return_value=self.adapter):
			# First delivery: not seen yet -> handle runs.
			self.adapter.already_processed.return_value = False
			spine.process("WATI", _WATI_INBOUND, _ACCOUNT, vendor_event=None, log=None)
			self.assertEqual(self.adapter.handle.call_count, 1)

			# Second, identical delivery: now seen -> handle must NOT run again.
			self.adapter.already_processed.return_value = True
			spine.process("WATI", _WATI_INBOUND, _ACCOUNT, vendor_event=None, log=None)
			self.assertEqual(self.adapter.handle.call_count, 1, "redelivery must not re-handle")

	# --- (e) replay() re-runs process from a stored Integration Request ---

	def test_replay_reruns_process_from_stored_request(self):
		"""spine.replay loads a stored raw Integration Request and re-drives process():
		correct service, payload recovered from `data`, event parsed out of
		request_description, account re-derived via the adapter's account_for_payload."""
		log = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "WATI",
				"request_description": "WATI",  # WATI carries no sub-event -> recovered as None
				"status": "Queued",
				"data": json.dumps(_WATI_INBOUND, default=str),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		self.adapter.account_for_payload.return_value = _ACCOUNT
		with patch.object(spine, "_adapter_for", return_value=self.adapter), \
		     patch.object(spine, "process") as proc:
			out = spine.replay(log.name)

		self.assertEqual(out, "ok")
		proc.assert_called_once()
		_args, kwargs = proc.call_args
		self.assertEqual(_args[0], "WATI")                       # service
		self.assertEqual(_args[1]["whatsappMessageId"], "wamid.TESTINBOUND001")  # payload
		self.assertEqual(_args[2], _ACCOUNT)                     # account re-derived
		self.assertIsNone(kwargs["vendor_event"])                # no sub-event for WATI
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
				spine.process("Acefone", {"call_id": "ACE-001"}, "Acefone Test Account",
				              vendor_event="inbound_complete", log=None)

	# --- (g) M1: a raising handle() flips the raw log to 'Failed' before re-raising

	def test_handler_exception_marks_log_failed(self):
		"""M1: when handle() raises, process() must mark its Integration Request 'Failed'
		(truthful DLQ row) and THEN re-raise — so replay_failed can later target it and the
		exception still reaches the RQ failed registry. The Queued->Failed flip is asserted
		on a real stored row."""
		log = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Acefone",
				"request_description": "Acefone inbound_complete",
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
				spine.process("Acefone", {"call_id": "ACE-001"}, "Acefone Test Account",
				              vendor_event="inbound_complete", log=log.name)

		self.assertEqual(
			frappe.db.get_value("Integration Request", log.name, "status"),
			"Failed",
			"a raised handle() must leave a truthful 'Failed' DLQ row",
		)

	# --- (h) M2: replay_failed re-enqueues ONLY status=='Failed' rows -----

	def test_replay_failed_targets_only_failed_rows(self):
		"""M2: replay_failed must re-enqueue exactly the Failed rows for a service and skip
		Queued/Completed ones — a still-in-flight (Queued) row is never replayed. Asserted via
		the names actually handed to enqueue (one per matching row)."""
		svc = "ReplayScopeSvc"
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
			count = spine.replay_failed(svc)

		self.assertEqual(count, 1, "only the single Failed row is in scope")
		self.assertEqual(enqueue.call_count, 1)
		_args, kwargs = enqueue.call_args
		self.assertEqual(_args[0], "tatva_connect.webhooks.spine.replay")
		self.assertEqual(kwargs["integration_request"], failed.name)


class TestAccountByToken(unittest.TestCase):
	"""H1: the shared, provider-agnostic token resolver fails CLOSED on a duplicate token —
	the SAME engine that backs BOTH providers, so the property holds for WATI and Acefone by
	construction. We drive it with each provider's real (account_doctype, token_field) config
	and stub the DB (get_all + get_cached_doc) so it runs without a bench."""

	# (account_doctype, token_field) for each provider — the only per-vendor inputs to the engine.
	_WATI_CFG = ("WhatsApp Account", "custom_webhook_token")
	_ACE_CFG = ("CRM Telephony Account", "webhook_token")

	def _run(self, cfg, token, stored_by_account):
		"""Resolve `token` against accounts whose stored secret is given by
		`stored_by_account` (name -> stored token or None), using provider `cfg`."""
		account_doctype, token_field = cfg
		names = list(stored_by_account)

		def fake_cached_doc(_doctype, name):
			doc = MagicMock()
			doc.get_password.return_value = stored_by_account[name]
			return doc

		with patch("frappe.get_all", return_value=names), \
		     patch("frappe.get_cached_doc", side_effect=fake_cached_doc):
			return routing.account_by_token(account_doctype, token_field, token)

	def test_unique_match_returns_account_wati(self):
		"""Exactly one account holds the token -> that account is returned (WATI config)."""
		out = self._run(self._WATI_CFG, "tok-aaa",
		                {"WA-One": "tok-aaa", "WA-Two": "tok-bbb"})
		self.assertEqual(out, "WA-One")

	def test_unique_match_returns_account_acefone(self):
		"""Exactly one account holds the token -> that account is returned (Acefone config)."""
		out = self._run(self._ACE_CFG, "tok-ddd",
		                {"ACE-One": "tok-ccc", "ACE-Two": "tok-ddd"})
		self.assertEqual(out, "ACE-Two")

	def test_duplicate_token_fails_closed_wati(self):
		"""H1 (WATI): two accounts share the token -> resolve None, never best-guess one and
		cross-attribute a tenant's inbound traffic."""
		out = self._run(self._WATI_CFG, "tok-dup",
		                {"WA-One": "tok-dup", "WA-Two": "tok-dup"})
		self.assertIsNone(out)

	def test_duplicate_token_fails_closed_acefone(self):
		"""H1 (Acefone): same fail-closed-on-duplicate property holds for telephony config —
		one shared engine, so the fix lands on both providers by construction."""
		out = self._run(self._ACE_CFG, "tok-dup",
		                {"ACE-One": "tok-dup", "ACE-Two": "tok-dup"})
		self.assertIsNone(out)

	def test_no_match_returns_none(self):
		"""No account holds the token -> None (fail-closed; unknown caller rejected)."""
		out = self._run(self._WATI_CFG, "tok-zzz",
		                {"WA-One": "tok-aaa", "WA-Two": "tok-bbb"})
		self.assertIsNone(out)

	def test_blank_token_returns_none(self):
		"""A blank/whitespace token short-circuits to None without scanning accounts."""
		self.assertIsNone(routing.account_by_token("WhatsApp Account", "custom_webhook_token", "   "))


class TestAssertSafePublicUrl(unittest.TestCase):
	"""M3: the single positive `is_global` SSRF test. We stub socket.getaddrinfo so a host
	resolves to a chosen IP and assert CGNAT (100.64.0.0/10) is blocked while a normal public
	host passes."""

	@staticmethod
	def _addrinfo(ip):
		"""A getaddrinfo()-shaped return: one (family, type, proto, canonname, sockaddr)
		5-tuple whose sockaddr (info[4][0]) is `ip` — the only field assert_safe_public_url reads."""
		return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]

	def test_cgnat_host_is_blocked(self):
		"""A host resolving to a 100.64.0.0/10 carrier-NAT address is non-global -> blocked.
		This is the range the old six-flag blocklist missed; the is_global rule catches it."""
		with patch("tatva_connect.utils.socket.getaddrinfo",
		           return_value=self._addrinfo("100.64.0.1")):
			with self.assertRaises(frappe.ValidationError):
				utils.assert_safe_public_url("https://cgnat.example.com/cdr.mp3")

	def test_public_host_passes(self):
		"""A host resolving to a normal global address passes (no exception)."""
		with patch("tatva_connect.utils.socket.getaddrinfo",
		           return_value=self._addrinfo("93.184.216.34")):
			try:
				utils.assert_safe_public_url("https://example.com/cdr.mp3")
			except frappe.ValidationError:
				self.fail("a public host must not be blocked")


if __name__ == "__main__":
	unittest.main()
