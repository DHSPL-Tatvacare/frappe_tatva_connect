# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The dashboard page calls OUR endpoint, and the endpoint answers the shape it reads.

An endpoint name is a string in a Vue file, so nothing enforced it and it drifted twice. `3b3938a`
pointed the page at `tatva_connect.dashboard.api.get_dashboard`; `5c1a6ac` — the commit that ADDED the
filter bar — put `crm.api.dashboard.get_dashboard` back. That endpoint takes no `filters` argument, so
frappe dropped it silently and every filter a user set did nothing; it returns a LIST where the page
reads `data.filters`, so the filter bar rendered empty whatever the layout exposed. Nothing went red:
a comment in the fork claiming "which is what the SPA now calls" cannot fail a build.

Two assertions, because the pair is what makes the page work: the page names this endpoint, and this
endpoint returns the keys the page reads off it.

Run:
    bench --site <site> run-tests --app tatva_connect \\
        --module tatva_connect.tests.dashboard.test_the_spa_calls_this_endpoint
"""
import os

import frappe
from frappe.tests.utils import FrappeTestCase

ENDPOINT = "tatva_connect.dashboard.api.get_dashboard"
RETIRED = "crm.api.dashboard.get_dashboard"
PAGE = "frontend/src/pages/Dashboard.vue"
# What Dashboard.vue reads off the answer: `dashboard.data?.filters`, `.charts`, `.configured`, `.title`.
READS = ("configured", "charts", "filters", "title")


class TestTheSpaCallsThisEndpoint(FrappeTestCase):
	def _page(self):
		path = os.path.join(frappe.get_app_path("crm"), "..", PAGE)
		if not os.path.exists(path):
			self.skipTest(f"{PAGE} not on this bench — nothing to assert")
		return open(path, encoding="utf-8").read()

	def test_the_dashboard_page_calls_this_endpoint(self):
		"""The page's resource url. Named here so a revert to the retired one stops a run, not a rep."""
		self.assertIn(ENDPOINT, self._page(), f"Dashboard.vue no longer calls {ENDPOINT}")

	def test_the_page_does_not_call_the_retired_endpoint(self):
		"""`crm.api.dashboard.get_dashboard` takes no `filters` and answers a list — both wrong here."""
		self.assertNotIn(RETIRED, self._page(), f"Dashboard.vue is calling the retired {RETIRED}")

	def test_this_endpoint_accepts_what_the_page_sends(self):
		"""from_date, to_date and filters — frappe drops an argument the signature does not name."""
		import inspect

		from tatva_connect.dashboard import api
		params = inspect.signature(api.get_dashboard).parameters
		for sent in ("from_date", "to_date", "filters"):
			self.assertIn(sent, params, f"the page sends `{sent}` and the endpoint would drop it")

	def test_this_endpoint_answers_the_keys_the_page_reads(self):
		"""A dict with these keys — not the retired endpoint's list, which has no `.filters` at all."""
		from tatva_connect.dashboard import api
		answer = api.get_dashboard()
		self.assertIsInstance(answer, dict, "the page reads `data.filters`; a list has none")
		missing = [k for k in READS if k not in answer]
		self.assertFalse(missing, f"the page reads {missing} and the endpoint does not return them")
