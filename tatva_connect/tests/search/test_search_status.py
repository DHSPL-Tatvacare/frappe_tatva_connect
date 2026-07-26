# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`search()` used to answer `{"results": [], "total": 0}` for three different situations — the toggle is
off, the index does not exist yet, and the user's words genuinely match nothing. The user reads all three
as "search is broken". P0 makes the middle one common: a schema change now legitimately drops the index
and leaves a real window where it is being rebuilt.

So the endpoint carries a `status` on EVERY return path — `disabled` / `building` / `ready` — derived from
the engine's own predicates (`is_search_enabled`, `index_exists`, `_is_indexing_complete`) and from nothing
else. The test that matters is the last one: a real no-match on a healthy index must still say `ready`,
because conflating "no hits" with "not ready" is exactly the lazy implementation this key exists to prevent.

No mocked index: this suite backs up the site's REAL index file, drops/builds it for real, and restores the
original in cleanup — the DB transaction rolls back, a file does not.

Run:
    bench --site uatreplay.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_search_status
"""
import os
import shutil

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.search.api import search
from tatva_connect.search.index import TOGGLE, CRMLeadSearch

# Long enough to clear the 3-char floor, and nothing in the corpus can contain it.
NO_MATCH = "zzqxvwmatchesnothing"


class TestSearchStatus(FrappeTestCase):
	def setUp(self):
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1)
		engine = CRMLeadSearch()
		self.db_path = engine.db_path
		self.backup = f"{self.db_path}.status-test.bak"
		if os.path.exists(self.db_path):
			shutil.copy2(self.db_path, self.backup)
		self.addCleanup(self._restore_site_index)

	def _restore_site_index(self):
		# Put the site's own index back byte for byte, whatever the test did to it.
		if os.path.exists(self.backup):
			shutil.move(self.backup, self.db_path)
		elif os.path.exists(self.db_path):
			os.unlink(self.db_path)

	def _ready_index(self):
		# A real, finished index — rebuilt only if the site's own one is absent or was cut short.
		engine = CRMLeadSearch()
		if not (engine.index_exists() and engine._is_indexing_complete()):
			engine.drop_index()
			engine.build_index()
		self.assertTrue(engine.index_exists())
		self.assertTrue(engine._is_indexing_complete())
		return engine

	def test_the_toggle_off_reports_disabled(self):
		# Dormant by default: the rest of the payload must still look exactly like it looks today.
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 0)
		payload = search("kavita")
		self.assertEqual(payload.get("status"), "disabled")
		self.assertEqual(payload["results"], [])
		self.assertEqual(payload["total"], 0)

	def test_an_absent_index_reports_building(self):
		CRMLeadSearch().drop_index()
		self.assertEqual(search("kavita").get("status"), "building")

	def test_a_cut_short_build_reports_building(self):
		self._ready_index()
		CRMLeadSearch().sql("UPDATE search_index_progress SET is_complete = 0", commit=True)
		self.assertEqual(search("kavita").get("status"), "building")

	def test_a_finished_index_reports_ready(self):
		self._ready_index()
		self.assertEqual(search("kavita").get("status"), "ready")

	def test_a_genuine_no_match_on_a_healthy_index_is_still_ready(self):
		# The one that catches conflating "no hits" with "not ready" — empty results, healthy index.
		self._ready_index()
		payload = search(NO_MATCH)
		self.assertEqual(payload["results"], [])
		self.assertEqual(payload["status"], "ready")

	def test_a_query_below_the_floor_says_so_rather_than_reporting_ready(self):
		# The endpoint owns the floor, so it must NAME it: the frontend holds no minimum and renders this status.
		# Reported "ready" before, which is indistinguishable from a genuine no-match and left the rule duplicated.
		self._ready_index()
		payload = search("ka")
		self.assertEqual(payload["results"], [])
		self.assertEqual(payload["status"], "too_short")

	def test_the_floor_is_the_endpoint_s_alone(self):
		# One clock: a query at the boundary is answered, one below it is refused, and both decisions are the
		# server's. If a second minimum ever grows on the client this test is what makes the drift visible.
		self._ready_index()
		self.assertEqual(search("k").get("status"), "too_short")
		self.assertEqual(search("ka").get("status"), "too_short")
		self.assertEqual(search("kav").get("status"), "ready")

	def test_dormant_beats_too_short(self):
		# A dormant feature is dormant whatever was typed — the operator toggle is never surfaced as a hint.
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 0)
		self.assertEqual(search("ka").get("status"), "disabled")
		self.assertEqual(search("kavita").get("status"), "disabled")
