# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`channels.transfer` — how bytes come across the wire, and what is refused on the way.

THIS MODULE HAD NO TESTS UNTIL 2026-08-05, and the cost of that is on the record: `read_capped` is the
size ceiling every provider download passes through, and `_fetch` refused redirects for months while every
call recording this app took was served behind one. Nothing here mocks the module under test — a fake
response is handed to it exactly as `requests` would.

THE TWO PROPERTIES THAT MATTER MOST are not "does it download". They are:
  * every hop is vetted, not only the first — the reason following a redirect is safe at all;
  * the Authorization header is dropped the moment the host changes — `requests` does this for you, and
    following hops by hand means it is ours to do or ours to leak.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.channels.test_transfer
"""
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from tatva_connect.channels import transfer

_BYTES = b"\xff\xe3H" + b"audio" * 64
_SAFE = "https://provider.example.com/recordings/abc"
_SAFE_OTHER_HOST = "https://cdn.example.net/signed/abc.mp3?sig=1"
_SAFE_SAME_HOST = "https://provider.example.com/signed/abc.mp3"
_PRIVATE = "http://169.254.169.254/latest/meta-data/"


class _Reply:
	"""One HTTP answer, in the shape the fetcher really reads: a status, headers, a stream, a close."""

	def __init__(self, status=200, content=_BYTES, content_type="audio/mpeg",
	             location=None, length=None):
		self.status_code = status
		self.content = content
		self.headers = {"Content-Type": content_type}
		if location:
			self.headers["Location"] = location
		if length is not None:
			self.headers["Content-Length"] = str(length)
		self.closed = False

	def raise_for_status(self):
		return None

	def close(self):
		self.closed = True

	def iter_content(self, chunk_size):
		for start in range(0, len(self.content), chunk_size):
			yield self.content[start:start + chunk_size]


class TransferCase(FrappeTestCase):
	"""Drives the real `fetch_capped`. Only the socket and the DNS/IP rule are replaced.

	`assert_safe_public_url` is patched with a RECORDING stub rather than switched off, because "which
	URLs were vetted" is the assertion in half these tests — a bare `patch(...)` would make every one of
	them pass whether or not the loop ever called it.
	"""

	def setUp(self):
		self.vetted = []
		guard = patch("tatva_connect.utils.assert_safe_public_url",
		              side_effect=lambda url, *a, **k: self.vetted.append(url))
		self.guard = guard.start()
		self.addCleanup(guard.stop)

	def fetch(self, replies, **kwargs):
		"""`fetch_capped` against a scripted sequence of answers.

		`self.got` is bound BEFORE the call, so a test that asserts on what was requested still has it
		after a refusal — which is exactly when that assertion matters most.
		"""
		self.replies = list(replies)
		with patch("requests.get", side_effect=lambda *a, **k: self.replies.pop(0)) as got:
			self.got = got
			return transfer.fetch_capped(_SAFE, timeout=5, **kwargs)


class TestARedirectIsTheDeliveryNotARefusal(TransferCase):
	"""A signed hop to object storage is HOW a recording is served. Refusing it reads an empty body."""

	def test_a_redirect_is_followed_and_the_body_at_the_end_is_returned(self):
		content, content_type = self.fetch([
			_Reply(status=307, location=_SAFE_OTHER_HOST, content=b""),
			_Reply(),
		])
		self.assertEqual(content, _BYTES)
		self.assertEqual(content_type, "audio/mpeg")

	def test_the_intermediate_response_is_closed_rather_than_left_holding_a_connection(self):
		hop = _Reply(status=307, location=_SAFE_OTHER_HOST, content=b"")
		self.fetch([hop, _Reply()])
		self.assertTrue(hop.closed, "a streamed 3xx holds its connection until it is read or closed")

	def test_a_relative_location_is_resolved_against_the_url_it_came_from(self):
		self.fetch([_Reply(status=302, location="/signed/abc.mp3", content=b""), _Reply()])
		self.assertEqual(self.vetted[-1], "https://provider.example.com/signed/abc.mp3")

	def test_a_redirect_naming_no_destination_is_refused(self):
		with self.assertRaises(ValueError) as raised:
			self.fetch([_Reply(status=302, content=b"")])
		self.assertIn("no destination", str(raised.exception))

	def test_a_chain_longer_than_the_bound_is_refused_rather_than_followed_for_ever(self):
		loop = [_Reply(status=302, location=_SAFE_OTHER_HOST, content=b"")
		        for _ in range(transfer.MAX_HOPS + 2)]
		with self.assertRaises(ValueError) as raised:
			self.fetch(loop)
		self.assertIn("redirected more than", str(raised.exception))


class TestEveryHopIsVettedNotOnlyTheFirst(TransferCase):
	"""The reason following a redirect is safe at all. Vetting the first host only is what the three
	call sites settled for, and it is why they refused the hop instead."""

	def test_the_destination_is_vetted_as_well_as_the_origin(self):
		self.fetch([_Reply(status=307, location=_SAFE_OTHER_HOST, content=b""), _Reply()])
		self.assertEqual(self.vetted, [_SAFE, _SAFE_OTHER_HOST])

	def test_a_hop_into_a_private_address_is_refused_at_the_hop_that_turns_bad(self):
		def vet(url, *a, **k):
			self.vetted.append(url)
			if url == _PRIVATE:
				raise ValueError("this call names an unsafe URL")

		self.guard.side_effect = vet
		with self.assertRaises(ValueError):
			self.fetch([_Reply(status=302, location=_PRIVATE, content=b""), _Reply()])
		self.assertIn(_PRIVATE, self.vetted, "the bad destination must be vetted, not assumed")
		self.assertEqual(self.got.call_count, 1, "the second GET must never leave the box")

	def test_the_operators_allowlist_applies_to_every_hop(self):
		self.fetch([_Reply(status=307, location=_SAFE_OTHER_HOST, content=b""), _Reply()],
		           allowed_hosts=["provider.example.com", "cdn.example.net"])
		self.assertEqual([call.args[1] for call in self.guard.call_args_list],
		                 [["provider.example.com", "cdn.example.net"]] * 2)


class TestTheCredentialDoesNotTravel(TransferCase):
	"""A provider signs its URL and redirects to storage on ANOTHER host. Carrying the bearer token
	across hands our credential to whoever the redirect names."""

	def test_the_authorization_header_is_dropped_when_the_host_changes(self):
		self.fetch([_Reply(status=307, location=_SAFE_OTHER_HOST, content=b""), _Reply()],
		           headers={"Authorization": "Bearer super-secret"})
		second = self.got.call_args_list[1].kwargs["headers"]
		self.assertNotIn("Authorization", second or {},
		                 "the provider's token must not reach the redirect's destination")

	def test_a_non_authorization_header_survives_the_hop(self):
		self.fetch([_Reply(status=307, location=_SAFE_OTHER_HOST, content=b""), _Reply()],
		           headers={"Authorization": "Bearer x", "X-Trace": "keep-me"})
		self.assertEqual(self.got.call_args_list[1].kwargs["headers"], {"X-Trace": "keep-me"})

	def test_the_token_is_KEPT_when_the_redirect_stays_on_the_same_host(self):
		"""Dropping it always would break a provider that signs within its own domain."""
		self.fetch([_Reply(status=307, location=_SAFE_SAME_HOST, content=b""), _Reply()],
		           headers={"Authorization": "Bearer keep"})
		self.assertEqual(self.got.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer keep")

	def test_the_first_request_carries_what_the_caller_gave_it(self):
		self.fetch([_Reply()], headers={"Authorization": "Bearer first"})
		self.assertEqual(self.got.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer first")


class TestTheCeilingIsStillTheOperatorsNumber(TransferCase):
	"""`read_capped`'s rules, reached through the fetcher, because that is how every caller reaches them."""

	def test_a_declared_length_over_the_ceiling_is_refused_before_a_byte_moves(self):
		with self.assertRaises(ValueError) as raised:
			self.fetch([_Reply(length=10_000_000)], max_bytes=1024)
		self.assertIn("declared", str(raised.exception))

	def test_a_body_that_passes_the_ceiling_while_reading_is_stopped(self):
		"""Content-Length is the sender's claim; a chunked response carries none at all."""
		with self.assertRaises(ValueError) as raised:
			self.fetch([_Reply(content=b"x" * 5000)], max_bytes=1024)
		self.assertIn("passed this site", str(raised.exception))

	def test_the_ceiling_defaults_to_what_the_SAVE_would_refuse(self):
		"""Not a constant here: a higher one buffers bytes only for `File.check_max_file_size` to refuse."""
		from frappe.core.api.file import get_max_file_size

		self.assertEqual(transfer.site_ceiling(), get_max_file_size())
