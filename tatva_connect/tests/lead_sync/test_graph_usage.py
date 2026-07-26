# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Meta publishes this app's utilisation on every response; we obey its number instead of modelling it.

The two header strings below are REAL — captured from live Graph calls against the GoodFlip app on
2026-07-26, one on a Page token and one on a user token. They differ structurally, which is why this is
worth a test: `x-app-usage` is a single object, while `x-business-use-case-usage` is keyed by object id
and holds a LIST per key. A parser written against either alone reads the other as nothing, and nothing
is indistinguishable from plenty of room.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_graph_usage
"""
import unittest
from unittest.mock import patch

import frappe

from tatva_connect.lead_sync import graph

# Live captures. Do not "tidy" these — their exact shape is the thing under test.
BUSINESS = '{"106777055130587":[{"type":"leadgen","call_count":1,"total_cputime":1,"total_time":1,"estimated_time_to_regain_access":0}]}'
APP = '{"call_count":2,"total_cputime":0,"total_time":2}'


def _response(**headers):
	return frappe._dict(headers=headers)


class TestGraphUsage(unittest.TestCase):

	# -- both real shapes are read -------------------------------------------

	def test_the_page_token_header_is_read(self):
		"""The business header holds a LIST per object id, not a bare record."""
		records = graph.usage_records(_response(**{"x-business-use-case-usage": BUSINESS}))
		self.assertEqual(len(records), 1)
		self.assertEqual(records[0]["type"], "leadgen")

	def test_the_user_token_header_is_read(self):
		"""The app header is a single object, not a list."""
		records = graph.usage_records(_response(**{"x-app-usage": APP}))
		self.assertEqual(len(records), 1)
		self.assertEqual(records[0]["call_count"], 2)

	def test_a_response_carrying_both_yields_both(self):
		both = _response(**{"x-business-use-case-usage": BUSINESS, "x-app-usage": APP})
		self.assertEqual(len(graph.usage_records(both)), 2)

	def test_a_response_with_no_usage_header_yields_nothing(self):
		self.assertEqual(graph.usage_records(_response()), [])

	def test_an_unreadable_header_never_fails_a_good_response(self):
		"""A malformed usage header is Meta's problem, not a reason to discard leads already fetched."""
		self.assertEqual(graph.usage_records(_response(**{"x-app-usage": "not json"})), [])

	# -- the decision --------------------------------------------------------

	def test_a_live_healthy_response_passes_through(self):
		"""The captured headers are a healthy account: nothing to stop, nothing written anywhere."""
		with patch.object(frappe, "log_error") as logged:
			graph.check_usage(_response(**{"x-business-use-case-usage": BUSINESS, "x-app-usage": APP}))
		self.assertFalse(logged.called)

	def test_a_throttled_response_stops_the_crawl_and_names_the_wait(self):
		"""Continuing past Meta's own block only collects the same rejection, so the call refuses."""
		shut = '{"106777055130587":[{"type":"leadgen","call_count":100,"estimated_time_to_regain_access":1800}]}'
		with self.assertRaises(frappe.ValidationError) as ctx:
			graph.check_usage(_response(**{"x-business-use-case-usage": shut}))
		self.assertIn("1800", str(ctx.exception), "the refusal must name the wait Meta gave")

	def test_high_utilisation_alone_never_stops_a_working_crawl(self):
		"""A percentage is not a verdict: a threshold of ours would refuse calls Meta still answers, and a
		row written from transport sits in the caller's transaction and can be rolled away without trace."""
		hot = '{"call_count":99,"total_cputime":99,"total_time":99}'
		with patch.object(frappe, "log_error") as logged:
			graph.check_usage(_response(**{"x-app-usage": hot}))  # must not raise
		self.assertFalse(logged.called)
