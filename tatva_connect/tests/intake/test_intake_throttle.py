# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The intake throttle, after it stopped being its own copy of the limiter.

`intake/guards._bump` was a hand-written fixed-window counter beside `utils.spend_rate_limit` — the
same primitive, twice. It now delegates, and these tests hold the two things that made it fork in the
first place, because both are invisible from the call site and neither had a test:

  * the cache key it counts under is unchanged, so a collapse does not reset live counters into a
    second window under a new name;
  * it refuses with a 417, not the limiter's own 429. Frappe's uploader shows the server message on
    403 and 417 alone, so a 429 reaches a visitor as "the file might be corrupted".

The second class holds the other half of the same contract: everybody who did NOT fork still gets the
429, so collapsing intake changed nothing for telephony or the documentation server.
"""
import unittest

import frappe

from tatva_connect.intake import guards
from tatva_connect.utils import spend_rate_limit


def _flush(key):
	frappe.cache.delete_value(key)


class TestIntakeThrottle(unittest.TestCase):
	def setUp(self):
		self.ident = frappe.generate_hash(length=8)
		self.key = f"intake-rl:ip:{self.ident}"
		_flush(self.key)
		self.addCleanup(_flush, self.key)

	def test_it_counts_under_the_key_it_always_counted_under(self):
		guards._bump("ip", self.ident, 5, 60)
		self.assertEqual(int(frappe.cache.get(frappe.cache.make_key(self.key))), 1)

	def test_it_refuses_at_the_limit(self):
		for _ in range(2):
			guards._bump("ip", self.ident, 2, 60)
		with self.assertRaises(frappe.ValidationError):
			guards._bump("ip", self.ident, 2, 60)

	def test_it_refuses_with_417_not_429_because_the_uploader_reads_only_that(self):
		guards._bump("ip", self.ident, 1, 60)
		with self.assertRaises(frappe.ValidationError) as refused:
			guards._bump("ip", self.ident, 1, 60)
		self.assertNotIsInstance(refused.exception, frappe.RateLimitExceededError)

	def test_the_refusal_tells_the_visitor_how_long_to_wait(self):
		"""The wait is formatted from the window, never typed — 3600 reads as frappe renders it."""
		frappe.clear_messages()
		guards._bump("ip", self.ident, 1, 3600)
		with self.assertRaises(frappe.ValidationError):
			guards._bump("ip", self.ident, 1, 3600)
		self.assertIn("1h", str(frappe.message_log[-1]))


class TestTheSharedLimiterIsUnchanged(unittest.TestCase):
	"""Telephony and the MCP server never forked; collapsing intake must not have moved them."""

	def setUp(self):
		self.ident = frappe.generate_hash(length=8)
		self.key = f"test-rl:subject:{self.ident}"
		_flush(self.key)
		self.addCleanup(_flush, self.key)

	def test_the_default_refusal_is_still_the_rate_limit_error(self):
		spend_rate_limit("test-rl:subject", self.ident, 1, 60, "no more")
		with self.assertRaises(frappe.RateLimitExceededError):
			spend_rate_limit("test-rl:subject", self.ident, 1, 60, "no more")

	def test_a_caller_under_its_limit_is_never_refused(self):
		for _ in range(5):
			spend_rate_limit("test-rl:subject", self.ident, 5, 60, "no more")
