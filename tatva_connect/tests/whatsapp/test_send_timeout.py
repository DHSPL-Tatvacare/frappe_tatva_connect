# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A7 — a WATI send must not wait for ever, and a send that got no answer is UNKNOWN, never failed.

`_post` used `make_post_request`, which cannot carry a timeout: frappe's `make_request`
(`integrations/utils.py:48`) declares no `timeout` and no `**kwargs`, so passing one is a `TypeError`
rather than a no-op — and there is no door behind it either, because `get_request_session` takes only
`max_retries` and `requests.Session` holds no default. A WATI outage therefore held a `workflow` worker
on an open socket until the OS gave up, with the whole send lane behind it.

NO NETWORK, AND NO MOCK OF THE THING UNDER TEST. These drive `_post` against a real local socket: one
that accepts and never answers (the shape a provider takes when it stops responding), and one that
answers 400 with a body. Asserting that a `timeout=` argument was passed would prove nothing about
whether it fires — so the assertion is that the call really comes back, and comes back as `OutcomeUnknown`.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.whatsapp.test_send_timeout
"""
import socket
import threading
import time
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from tatva_connect.whatsapp import transport

# Well under the shipped SEND_TIMEOUT, so a test that has to wait for a timeout still runs in a second.
_PATCHED_TIMEOUT = 1

# The ceiling the timeout assertion is really making: anything under this proves the socket was let go
# deliberately rather than by the OS, whose own connect/read give-up is measured in minutes.
_MUST_RETURN_WITHIN = 15


class _Provider:
	"""A real listening socket. With no `reply` it accepts and stays silent for ever; with one it answers.

	A silent-but-connected peer is the case a connect timeout cannot catch and the one that actually
	happens — WATI accepts the TCP connection and then never writes a response.
	"""

	def __init__(self, reply=None):
		self.reply = reply
		self._listener = socket.socket()
		self._listener.bind(("127.0.0.1", 0))
		self._listener.listen(2)
		self._open = []
		self._thread = threading.Thread(target=self._serve, daemon=True)
		self._thread.start()

	@property
	def url(self):
		return f"http://127.0.0.1:{self._listener.getsockname()[1]}"

	def _serve(self):
		while True:
			try:
				conn, _peer = self._listener.accept()
			except OSError:
				return  # the listener was closed by teardown
			if self.reply is None:
				self._open.append(conn)  # held open and never written to
				continue
			try:
				conn.recv(65536)
				conn.sendall(self.reply)
			finally:
				conn.close()

	def close(self):
		self._listener.close()
		for conn in self._open:
			conn.close()


def _http(status, body):
	return (
		f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
		f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
	).encode() + body


class TestASendThatGetsNoAnswerComesBack(FrappeTestCase):
	def setUp(self):
		self.provider = _Provider()
		self.addCleanup(self.provider.close)

	def test_a_provider_that_accepts_and_never_answers_times_out(self):
		"""The defect, as an outcome: before the fix this call does not return at all."""
		started = time.monotonic()

		with patch.object(transport, "SEND_TIMEOUT", _PATCHED_TIMEOUT):
			with self.assertRaises(transport.OutcomeUnknown):
				transport._post(f"{self.provider.url}/api/v1/sendTemplateMessage", "probe-token", {"x": 1})

		self.assertLess(
			time.monotonic() - started, _MUST_RETURN_WITHIN,
			"the send held the worker on a provider that never answered",
		)

	def test_no_answer_is_unknown_and_never_a_refusal(self):
		"""`OutcomeUnknown` is the whole point: classified as failed, a rep clicks Send again and the
		patient gets one clinical message twice. A timeout does not mean the message did not go."""
		with patch.object(transport, "SEND_TIMEOUT", _PATCHED_TIMEOUT):
			with self.assertRaises(transport.OutcomeUnknown):
				transport._post(f"{self.provider.url}/x", "probe-token", {})

	def test_a_send_is_not_replayed_on_the_way_to_the_timeout(self):
		"""`get_request_session` mounts `Retry(total=5, status_forcelist=[500])`. POST is not in urllib3's
		retryable set, so a send is never re-sent — the timeout arrives at roughly its own budget, not at
		six times it. This is why going through frappe's session on a send path is safe."""
		started = time.monotonic()

		with patch.object(transport, "SEND_TIMEOUT", _PATCHED_TIMEOUT):
			with self.assertRaises(transport.OutcomeUnknown):
				transport._post(f"{self.provider.url}/x", "probe-token", {})

		self.assertLess(
			time.monotonic() - started, _PATCHED_TIMEOUT * 3,
			"the send was retried on its way to the timeout, so a patient may have been messaged twice",
		)


class TestAnAnswerIsStillAnAnswerWhateverItsStatus(FrappeTestCase):
	"""The regression guard on the rewrite: `raise_for_status` is gone, so these pin the contract it used
	to route — WATI signals refusals both as 200 + `result: false` and as a 4xx, and BOTH are answers."""

	def _answering(self, status, body):
		provider = _Provider(reply=_http(status, body))
		self.addCleanup(provider.close)
		return transport._post(f"{provider.url}/x", "probe-token", {})

	def test_a_4xx_body_is_returned_rather_than_raised(self):
		answer = self._answering("400 Bad Request", b'{"result": false, "info": "template needs params"}')

		self.assertEqual(answer, {"result": False, "info": "template needs params"})

	def test_a_200_body_is_returned(self):
		answer = self._answering("200 OK", b'{"result": true}')

		self.assertEqual(answer, {"result": True})

	def test_an_unreadable_body_is_a_refusal_and_not_an_unknown(self):
		"""A response that cannot be parsed is still a response. Turning it into `OutcomeUnknown` would
		say "we do not know" about a provider that answered — `send_session_file` already settled this."""
		answer = self._answering("200 OK", b"<html>maintenance</html>")

		self.assertFalse(answer["result"])
		self.assertIn("maintenance", answer["info"])
