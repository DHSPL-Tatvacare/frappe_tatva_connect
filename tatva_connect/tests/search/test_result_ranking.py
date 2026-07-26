# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""There is ONE ranking, and it lives where the framework puts ranking.

The endpoint used to sort a THIRD time: the framework sorts raw rows by bm25 (`sqlite_search.py:904`), then
by its own composite score (`:946`), and then `api.search` re-sorted the shaped hits by a `_RANK` doctype
map. Three sorts, one of which silently owned the outcome. The doctype preference now rides the framework's
own seam — `get_scoring_pipeline()` — so `api.search` sorts nothing and the order is the engine's.

What this suite locks:

  1. **The order itself.** leads -> notes -> attachments, asserted on one query that matches all three,
     straight off the engine AND off the endpoint. It is the owner's deliberate order and the reason
     the sort could not simply be deleted. (CRM Task is deliberately NOT indexed — see `INDEXABLE_DOCTYPES`.)
  2. **That the pipeline, not a post-sort, produces it** — the red case restores the framework's own
     `get_scoring_pipeline` and shows the order collapse.
  3. **No silent conditional on recency.** `get_scoring_pipeline` in the framework appends the recency boost
     only when `modified` is a metadata field, and ours does not declare one. Declared or not is a decision;
     this asserts the decision holds either way — our pipeline is the same three functions even if `modified`
     appears in the schema, so nothing can turn on by accident.
  4. **An honest count.** `total` is capped at `MAX_SEARCH_RESULTS`, so a plateaued number is a floor and the
     response must say so (`total_capped`), or the UI reads 100 as exact. With CRM Task out of the index this
     site no longer holds 100 matchable rows, so the boundary is driven by lowering the cap — stated in place.
  5. **The server's own `<mark>`.** FTS5 prefix-matches `rames` to `Ramesh` and marks the WHOLE token; the
     client regex that used to re-highlight could only ever mark `Rames`. The marked token is asserted here.

No mocked index: the site's real index file is backed up, rebuilt for real against minted records, and
restored in cleanup — the DB transaction rolls back, a file does not.

Run:
    bench --site uatreplay.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_result_ranking
"""
import os
import shutil
from unittest.mock import patch

import frappe
from frappe.search.sqlite_search import MAX_SEARCH_RESULTS, SQLiteSearch
from frappe.tests.utils import FrappeTestCase

from tatva_connect.search import api as search_api
from tatva_connect.search.index import TOGGLE, CRMLeadSearch

# One token carried by a lead, a note and a file, so ONE query matches every indexed doctype exactly once.
TOKEN = "zzrankpatient"
PHONE_PREFIX = "+91610009"

# A name whose PREFIX is what gets typed — the case a client-side regex on the typed term cannot mark.
FULL_FIRST_NAME = "Rameshzz"

# The order the owner chose. Not derived from the code under test on purpose: this list IS the requirement.
EXPECTED_ORDER = ["CRM Lead", "FCRM Note", "File"]


class TestResultRanking(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		cls.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": f"{FULL_FIRST_NAME} {TOKEN}", "last_name": "Rank",
			"status": "New", "mobile_no": f"{PHONE_PREFIX}0001",
		}).insert(ignore_permissions=True).name
		cls.note = frappe.get_doc({
			"doctype": "FCRM Note", "title": f"{TOKEN} note", "content": f"<p>{TOKEN} body</p>",
			"reference_doctype": "CRM Lead", "reference_docname": cls.lead,
		}).insert(ignore_permissions=True).name
		# A task carrying the same token, minted on purpose: it must NOT appear in any result below.
		cls.task = frappe.get_doc({
			"doctype": "CRM Task", "title": f"{TOKEN} task", "description": f"<p>{TOKEN} body</p>",
			"status": "Backlog", "priority": "Low",
			"reference_doctype": "CRM Lead", "reference_docname": cls.lead,
		}).insert(ignore_permissions=True).name
		cls.file = frappe.get_doc({
			"doctype": "File", "file_name": f"{TOKEN}.txt", "is_private": 1,
			"content": "rank fixture", "attached_to_doctype": "CRM Lead", "attached_to_name": cls.lead,
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"{PHONE_PREFIX}%"]}, pluck="name"):
			for doctype in ("CRM Task", "FCRM Note"):
				for child in frappe.get_all(doctype, filters={"reference_docname": name}, pluck="name"):
					frappe.delete_doc(doctype, child, force=True, ignore_permissions=True)
			for child in frappe.get_all("File", filters={"attached_to_name": name}, pluck="name"):
				frappe.delete_doc("File", child, force=True, ignore_permissions=True)
			frappe.db.delete("ToDo", {"reference_type": "CRM Lead", "reference_name": name})
			frappe.db.delete("DocShare", {"share_doctype": "CRM Lead", "share_name": name})
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1)
		frappe.db.set_value("CRM Tatva Automation", search_api.SPLIT_TOGGLE, "enabled", 0)
		engine = CRMLeadSearch()
		self.db_path = engine.db_path
		self.backup = f"{self.db_path}.rank-test.bak"
		if os.path.exists(self.db_path):
			shutil.copy2(self.db_path, self.backup)
		self.addCleanup(self._restore_site_index)
		engine.drop_index()
		engine.build_index()

	def _restore_site_index(self):
		if os.path.exists(self.backup):
			shutil.move(self.backup, self.db_path)
		elif os.path.exists(self.db_path):
			os.unlink(self.db_path)

	def _engine_order(self, query=TOKEN):
		res = CRMLeadSearch().search(query) or {}
		return [r.get("doctype") for r in res.get("results", [])]

	def _endpoint_order(self, query=TOKEN):
		return [hit["doctype"] for hit in search_api.search(query)["results"]]

	# --- 1. the order, which is the requirement ---------------------------------------------------------

	def test_the_declared_doctype_order_is_what_the_engine_returns(self):
		self.assertEqual(self._engine_order(), EXPECTED_ORDER)

	def test_the_endpoint_returns_the_same_order_without_sorting_anything(self):
		"""The third sort is gone, so these two must agree by construction — if they diverge, it grew back."""
		self.assertEqual(self._endpoint_order(), EXPECTED_ORDER)
		self.assertEqual(self._endpoint_order(), self._engine_order())

	def test_the_order_is_the_declaration_order_and_nothing_else(self):
		"""ONE place declares it: INDEXABLE_DOCTYPES. Reorder the declaration and the results reorder with it."""
		self.assertEqual([dt for dt in CRMLeadSearch.INDEXABLE_DOCTYPES], EXPECTED_ORDER)
		flipped = {dt: CRMLeadSearch.INDEXABLE_DOCTYPES[dt] for dt in reversed(EXPECTED_ORDER)}
		with patch.object(CRMLeadSearch, "INDEXABLE_DOCTYPES", flipped):
			self.assertEqual(self._engine_order(), list(reversed(EXPECTED_ORDER)))

	def test_the_task_carrying_the_same_token_is_absent_from_the_index(self):
		"""CRM Task came OUT — 12,567 rows / 2.93 MB the owner does not want. The fixture task carries the SAME
		token as every row above, so its absence from the hit list and from the table is the removal, proven."""
		self.assertNotIn("CRM Task", CRMLeadSearch.INDEXABLE_DOCTYPES)
		self.assertNotIn("CRM Task", self._engine_order())
		rows = CRMLeadSearch().sql("SELECT COUNT(*) c FROM search_fts WHERE doctype = 'CRM Task'", read_only=True)
		self.assertEqual(rows[0]["c"], 0, "CRM Task rows are still in the index")
		self.assertTrue(frappe.db.exists("CRM Task", self.task), "fixture: the task must exist or this proved nothing")

	# --- 2. the RED proof: the pipeline is what produces it ---------------------------------------------

	def test_without_the_pipelines_doctype_tier_the_order_collapses(self):
		"""The framework's own pipeline, restored: bm25 + title boost put a note or a task above the lead, which
		is exactly why `api.py` had to re-sort. If this ever passes, the tier stopped being load-bearing."""
		with patch.object(CRMLeadSearch, "get_scoring_pipeline", SQLiteSearch.get_scoring_pipeline):
			collapsed = self._engine_order()
		self.assertNotEqual(collapsed, EXPECTED_ORDER, "the framework's own ranking already produced the owner's order")
		self.assertEqual(sorted(collapsed), sorted(EXPECTED_ORDER), "the same four hits must be present, only reordered")

	def test_a_whole_doctype_is_never_overtaken_by_a_stronger_text_match(self):
		"""The tier is a magnitude, not a tie-break: the note matches the token in BOTH its text fields and the
		lead matches it in one, so a bm25-driven order would promote the note past the lead."""
		res = CRMLeadSearch().search(TOKEN) or {}
		scores = {r["doctype"]: r["score"] for r in res["results"]}
		self.assertGreater(scores["CRM Lead"], scores["FCRM Note"])
		self.assertGreater(scores["FCRM Note"], scores["File"])

	# --- 3. recency: a decision, not a silent conditional -----------------------------------------------

	def test_the_recency_boost_is_absent_and_stays_absent_even_if_modified_appears(self):
		"""`modified` is deliberately not declared. The framework would switch the boost on from the schema
		alone; our pipeline is written out, so the decision cannot be flipped by an unrelated schema edit."""
		engine = CRMLeadSearch()
		self.assertNotIn("modified", engine.schema["metadata_fields"])
		names = [f.__name__ for f in engine.get_scoring_pipeline()]
		self.assertEqual(names, ["_get_base_score", "_get_title_boost", "_doctype_tier"])

		# What declaring it really costs, for the record: the framework's own `_validate_config` demands the
		# field be selected for EVERY indexed doctype too, so both declarations move together.
		with_modified = {**CRMLeadSearch.INDEX_SCHEMA, "metadata_fields": [*CRMLeadSearch.INDEX_SCHEMA["metadata_fields"], "modified"]}
		doctypes = {
			doctype: {**config, "fields": [*config["fields"], "modified"]}
			for doctype, config in CRMLeadSearch.INDEXABLE_DOCTYPES.items()
		}
		with patch.object(CRMLeadSearch, "INDEX_SCHEMA", with_modified), \
			patch.object(CRMLeadSearch, "INDEXABLE_DOCTYPES", doctypes):
			engine = CRMLeadSearch()
			self.assertIn("modified", engine.schema["metadata_fields"])
			self.assertEqual([f.__name__ for f in engine.get_scoring_pipeline()], names, "recency switched itself on")

	# --- 4. an honest count -----------------------------------------------------------------------------

	def test_a_plateaued_total_is_reported_as_a_floor(self):
		"""The boundary is driven by LOWERING the cap, not by seeding 100 leads: with CRM Task out of the index
		this site holds ~156 rows, so no real query can reach MAX_SEARCH_RESULTS any more. The endpoint's own
		rule — `total >= the cap` means the number is a floor — is what is under test, and it is driven both ways."""
		narrow = search_api.search(TOKEN)
		self.assertEqual(narrow["total"], len(EXPECTED_ORDER))
		self.assertFalse(narrow["total_capped"], "a three-hit search must report an exact count")

		with patch.object(search_api, "MAX_SEARCH_RESULTS", len(EXPECTED_ORDER)):
			capped = search_api.search(TOKEN)
		self.assertEqual(capped["total"], len(EXPECTED_ORDER))
		self.assertTrue(capped["total_capped"], "a total that reached the cap was reported as if it were exact")
		self.assertEqual(MAX_SEARCH_RESULTS, 100, "the framework's own cap moved — the UI's `100+` no longer reads true")

	# --- 5. the server's own <mark> ----------------------------------------------------------------------

	def test_a_prefix_match_marks_the_whole_token_the_client_regex_could_not(self):
		"""Type `rames`: FTS5 prefix-matches and marks `Rameshzz`. A regex on the typed term marks `Rames` only."""
		res = CRMLeadSearch().search("rames") or {}
		titles = [r["title"] for r in res["results"] if r["doctype"] == "CRM Lead"]
		self.assertTrue(titles, "the prefix query matched no lead — the highlight assertion proved nothing")
		self.assertTrue(
			any(f"<mark>{FULL_FIRST_NAME}</mark>" in title for title in titles),
			f"the framework marked something other than the whole token: {titles}",
		)
		# What the deleted client regex would have produced instead, stated so the difference is on the record.
		self.assertNotIn("<mark>Rames</mark>", " ".join(titles))

	def test_the_endpoint_passes_the_marked_text_through_untouched(self):
		hit = next(h for h in search_api.search("rames")["results"] if h["doctype"] == "CRM Lead")
		self.assertIn("<mark>", hit["title"], "the endpoint stripped the framework's own highlight")
