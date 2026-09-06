# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A dashboard tile carrying no filters is the NORMAL case, and it used to be a 500.

A number card and a custom chart both post `filters: null`. That arrives at the server as the empty
STRING, not as None — and `frappe.parse_json("")` raises `JSONDecodeError`. Every custom tile on the
Observability page therefore rendered "Loading..." for ever, with a traceback naming this app, and it
read exactly like missing data: the page had just been armed, the rollup was behind, and nobody
questioned a zero. It was never the data.

The bug survived because the only thing that ever called these endpoints was a test passing `None`,
which is not a string and skips the parse entirely. So this file asserts the SHAPE THE BROWSER SENDS
— `""` — on every endpoint that funnels through `_card`, plus the chart source that had the identical
line. `None` and a real filter dict are asserted beside it, so a fix that only moved the crash is
caught too.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.observability import analytics
from tatva_connect.observability.dashboard_chart_source.api_observability import api_observability

# What a browser actually posts, and the two shapes a server-side caller sends.
_AS_SENT = ("", "   ", None, {"channel": "Partner API"})

_CARDS = (analytics.error_rate_card, analytics.p95_card,
          analytics.p99_card, analytics.avg_latency_card)


class TestATileWithNoFiltersIsAnswered(FrappeTestCase):
	def test_every_card_endpoint_answers_the_empty_string(self):
		"""The exact payload from the page. Each must return a value, not raise."""
		for card in _CARDS:
			for sent in _AS_SENT:
				with self.subTest(card=card.__name__, filters=repr(sent)):
					out = card(filters=sent)
					self.assertIn("value", out)
					self.assertIn("fieldtype", out)

	def test_the_chart_source_answers_it_too(self):
		"""It carried the identical line, so it carries the identical assertion."""
		for sent in _AS_SENT:
			with self.subTest(filters=repr(sent)):
				out = api_observability.get(chart_name="API p95 Latency (Daily)", filters=sent, no_cache=1)
				self.assertIn("labels", out)
				self.assertIn("datasets", out)

	def test_blank_and_absent_mean_the_same_thing(self):
		"""`""` is not a filter the caller chose — it must read as no filter, not as an empty one."""
		self.assertIsNone(analytics.parse_filters(""))
		self.assertIsNone(analytics.parse_filters("   "))
		self.assertIsNone(analytics.parse_filters(None))
		self.assertEqual(analytics.parse_filters('{"channel": "Partner API"}'), {"channel": "Partner API"})
		self.assertEqual(analytics.parse_filters({"channel": "X"}), {"channel": "X"})
