# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Graph transport: the token rides a header, and a failure explains itself honestly.

Two things this locks. The token never travels as a query param, because Meta echoes the URL into its own
error text and frappe writes that into the Error Log in plaintext. And a failure names THIS call's
response, never the previous one: `frappe.flags.integration_request` is only set once a response exists,
so a retried 5xx or a connection drop left the last successful call's status and body standing in for it,
which reported "HTTP 200" for a call that never reached Facebook.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_graph_transport
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync import graph


class _Response:
	def __init__(self, status_code, payload, text=""):
		self.status_code = status_code
		self._payload = payload
		self.text = text

	def json(self):
		return self._payload


class TestTokenTransport(FrappeTestCase):
	def test_the_token_rides_the_authorization_header(self):
		self.assertEqual(graph.bearer("zz-token"), {"Authorization": "Bearer zz-token"})

	def test_no_token_means_no_header(self):
		self.assertEqual(graph.bearer(""), {})

	def test_a_caller_supplied_token_param_is_dropped(self):
		"""The header is the only carrier, so a stray param is not trusted alongside it."""
		self.assertEqual(graph.strip_token({"access_token": "zz", "fields": "id"}), {"fields": "id"})

	def test_the_token_is_stripped_from_a_paging_url(self):
		"""Graph's `next` is a complete URL carrying its own access_token in the query string."""
		url = "https://graph.facebook.com/v23.0/x/leads?access_token=zz-token&after=CURSOR&limit=100"
		stripped = graph.strip_token_from_url(url)
		self.assertNotIn("access_token", stripped)
		self.assertIn("after=CURSOR", stripped)
		self.assertIn("limit=100", stripped)

	def test_a_url_without_a_query_is_untouched(self):
		url = "https://graph.facebook.com/v23.0/me"
		self.assertEqual(graph.strip_token_from_url(url), url)

	def test_the_token_reaches_the_request_as_a_header_and_not_a_param(self):
		seen = {}

		def _capture(url, params=None, headers=None):
			seen["url"], seen["params"], seen["headers"] = url, params, headers
			return {"ok": True}

		with patch.object(graph, "make_get_request", _capture):
			graph.graph_get("probe", "https://graph.facebook.com/v23.0/me", {"fields": "id"}, "zz-token")

		self.assertEqual(seen["headers"], {"Authorization": "Bearer zz-token"})
		self.assertNotIn("access_token", seen["params"])
		self.assertNotIn("zz-token", seen["url"])


def _fails_with(response):
	"""How frappe's make_request really behaves: the flag is set once a response exists, then it raises."""

	def _raise(*args, **kwargs):
		frappe.flags.integration_request = response
		raise RuntimeError("boom")

	return _raise


class TestFailureReporting(FrappeTestCase):
	def test_metas_reason_is_surfaced(self):
		response = _Response(400, {"error": {"message": "Invalid OAuth access token", "code": 190}})
		with patch.object(graph, "make_get_request", _fails_with(response)):
			with self.assertRaises(frappe.ValidationError) as caught:
				graph.graph_get("probe", "https://graph.facebook.com/v23.0/me", {}, "zz-token")
		self.assertIn("Invalid OAuth access token", str(caught.exception))
		self.assertIn("error code 190", str(caught.exception))

	def test_a_failure_with_no_response_does_not_borrow_the_previous_one(self):
		"""The defect this locks: a retried 5xx reported the last successful call's 200 and its body."""
		stale = _Response(200, {"data": {"type": "USER"}}, text='{"data": {"type": "USER"}}')
		frappe.flags.integration_request = stale
		with patch.object(graph, "make_get_request", side_effect=RuntimeError("Max retries exceeded")):
			with self.assertRaises(frappe.ValidationError) as caught:
				graph.graph_get("token exchange", "https://graph.facebook.com/v23.0/oauth", {}, "zz-token")
		message = str(caught.exception)
		self.assertNotIn("HTTP 200", message, "a failed call must not report the previous call's status")
		self.assertNotIn("USER", message, "a failed call must not report the previous call's body")
		self.assertIn("Max retries exceeded", message, "the real cause has to be named")

	def test_the_token_is_never_echoed_back_in_the_message(self):
		"""Meta repeats the token inside its own error text, and that text reaches the Error Log."""
		response = _Response(400, {"error": {"message": "Bad token EAAsecret123456789012 supplied"}})
		with patch.object(graph, "make_get_request", _fails_with(response)):
			with self.assertRaises(frappe.ValidationError) as caught:
				graph.graph_get("probe", "https://graph.facebook.com/v23.0/me", {}, "EAAsecret123456789012")
		self.assertNotIn("EAAsecret123456789012", str(caught.exception))
