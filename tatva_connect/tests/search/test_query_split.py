# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Two lanes, one box: words the system already KNOWS become index filters, everything left over stays text.

The whole feature is behind `Search::Query::vocabulary`, dormant. The first test in this file is therefore the
most important one: with the toggle off the response is **byte-identical** to the one produced when nothing
resolves — which is today's product. "Flag off ⇒ X doesn't happen" is the requirement, not a limitation.

Three decisions this suite pins down, because each one has a failure mode that returns zero rows in silence:

  1. **Two values for the SAME column** ("onco liver kavita") would be `vertical = A AND vertical = B` — an
     empty set by construction. The first reading wins and the rest goes back to the text lane.
  2. **An ambiguous term** (a word that is both a vertical and a group) resolves to NOTHING and its words go
     to the text lane — `vocabulary.match` refuses to guess, and the endpoint must not guess either.
  3. **A query with nothing left for text** ("onco" alone) is answered exactly as today. FTS5 has no
     match-everything, so a filter can only NARROW a text search; applying it with an empty query returns
     nothing at all, which would read as "understood you, found nothing" — a lie.

And the security assertion: a caller-supplied filter can never widen scope. The framework merges permission
filters LAST (`sqlite_search.py:262`), and that is asserted on the outcome, not on the merge.

No mocked index: the site's real index file is backed up, rebuilt for real against minted records, and
restored in cleanup — the DB transaction rolls back, a file does not.

Run:
    bench --site uatreplay.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_query_split
"""
import json
import os
import shutil
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import seed
from tatva_connect.automation.registry import AUTOMATIONS
from tatva_connect.search import api as search_api
from tatva_connect.search import index as search_index
from tatva_connect.search import vocabulary
from tatva_connect.search.index import TOGGLE, CRMLeadSearch

TOKEN = "zzsplitpatient"
PHONE_PREFIX = "+91610010"

VERTICAL_A = "ZZ Split Onco"
VERTICAL_B = "ZZ Split Liver"
GROUP = "ZZ Split Group"
# One spelling that is BOTH a vertical and a group — the ambiguity the matcher refuses to resolve.
BOTH = "ZZ Split Ambiguous"

REP = "zz-split-rep@example.com"
OTHER = "zz-split-other@example.com"


class TestQuerySplit(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		for vertical in (VERTICAL_A, VERTICAL_B, BOTH):
			if not frappe.db.exists("CRM Vertical", vertical):
				frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": vertical}).insert(ignore_permissions=True)
		for group in (GROUP, BOTH):
			if not frappe.db.exists("CRM Group", group):
				frappe.get_doc({"doctype": "CRM Group", "group_name": group}).insert(ignore_permissions=True)
		for email in (REP, OTHER):
			if not frappe.db.exists("User", email):
				user = frappe.get_doc({
					"doctype": "User", "email": email, "first_name": "Split",
					"send_welcome_email": 0, "user_type": "System User",
				}).insert(ignore_permissions=True)
				user.append("roles", {"role": "Sales User"})
				user.save(ignore_permissions=True)

		# The same patient name in two verticals: the only thing that can separate them is the filter.
		cls.in_a = cls._lead(VERTICAL_A, 1)
		cls.in_b = cls._lead(VERTICAL_B, 2)
		cls.toggle_created = not frappe.db.exists("CRM Tatva Automation", search_api.SPLIT_TOGGLE)
		if cls.toggle_created:
			# Built from the registry row itself, through the seed's own writer — no structural value restated.
			auto = next(a for a in AUTOMATIONS if a.key == search_api.SPLIT_TOGGLE)
			doc = frappe.new_doc("CRM Tatva Automation")
			doc.automation_key = auto.key
			for field, value in seed._structural_values(auto).items():
				doc.set(field, value)
			doc.enabled = 0
			doc.insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, restores the row it created
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		if cls.toggle_created and frappe.db.exists("CRM Tatva Automation", search_api.SPLIT_TOGGLE):
			frappe.delete_doc("CRM Tatva Automation", search_api.SPLIT_TOGGLE, force=True, ignore_permissions=True)
		else:
			frappe.db.set_value("CRM Tatva Automation", search_api.SPLIT_TOGGLE, "enabled", 0)
		for email in (REP, OTHER):
			frappe.db.delete("User Permission", {"user": email})
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		for doctype, names in (("CRM Vertical", (VERTICAL_A, VERTICAL_B, BOTH)), ("CRM Group", (GROUP, BOTH))):
			for name in names:
				if frappe.db.exists(doctype, name):
					frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"{PHONE_PREFIX}%"]}, pluck="name"):
			frappe.db.delete("ToDo", {"reference_type": "CRM Lead", "reference_name": name})
			frappe.db.delete("DocShare", {"share_doctype": "CRM Lead", "share_name": name})
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	@classmethod
	def _lead(cls, vertical, seq):
		doc = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": TOKEN, "last_name": f"S{seq}", "status": "New",
			"mobile_no": f"{PHONE_PREFIX}{seq:04d}", "custom_vertical": vertical, "custom_group": GROUP,
		}).insert(ignore_permissions=True)
		frappe.db.set_value("CRM Lead", doc.name, "lead_owner", REP, update_modified=False)
		return doc.name

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(vocabulary._vocabulary.clear_cache)
		vocabulary._vocabulary.clear_cache()
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1)
		self._split(False)
		self.addCleanup(self._split, False)
		engine = CRMLeadSearch()
		self.db_path = engine.db_path
		self.backup = f"{self.db_path}.split-test.bak"
		if os.path.exists(self.db_path):
			shutil.copy2(self.db_path, self.backup)
		self.addCleanup(self._restore_site_index)
		engine.drop_index()
		engine.build_index()
		for email in (REP, OTHER, "Administrator"):
			frappe.set_user(email)
			search_index.visible_principals.clear_cache()
		frappe.set_user("Administrator")

	def _restore_site_index(self):
		if os.path.exists(self.backup):
			shutil.move(self.backup, self.db_path)
		elif os.path.exists(self.db_path):
			os.unlink(self.db_path)

	def _split(self, enabled):
		frappe.db.set_value("CRM Tatva Automation", search_api.SPLIT_TOGGLE, "enabled", 1 if enabled else 0)

	def _leads(self, response):
		return {hit["lead"] for hit in response["results"]}

	# --- 1. dormant means identical -----------------------------------------------------------------------

	def test_with_the_toggle_off_a_recognised_word_is_just_text(self):
		off = search_api.search(f"{VERTICAL_A} {TOKEN}")
		self.assertNotIn("understood", off, "the dormant response gained a key")
		self.assertEqual(set(off), {"results", "total", "status", "total_capped"})

	def test_the_toggle_off_response_is_byte_identical_to_a_query_that_resolves_nothing(self):
		"""The plan's failure mode IS the current product: nothing recognised ⇒ no filters ⇒ today's spotlight.
		So dormant and "on but nothing matched" must produce the same bytes, or one of the two lanes is lying.
		Driven on a query that really returns rows, so the identity covers the shaped hits and not just an
		empty envelope."""
		rich = search_api.search(TOKEN)
		self.assertTrue(rich["results"], "fixture: this query must return rows or the identity proves nothing")

		for query in (TOKEN, f"{VERTICAL_A} {TOKEN}"):
			off = json.dumps(search_api.search(query), sort_keys=True, default=str)
			self._split(True)
			with patch.object(vocabulary, "_vocabulary", lambda: frappe._dict(terms={}, span=1)):
				nothing_resolved = json.dumps(search_api.search(query), sort_keys=True, default=str)
			# And with the REAL vocabulary, a query it recognises nothing in must be identical too.
			live = json.dumps(search_api.search(TOKEN), sort_keys=True, default=str)
			self._split(False)
			self.assertEqual(off, nothing_resolved, f"{query!r} differed with the toggle off")
			self.assertEqual(json.dumps(rich, sort_keys=True, default=str), live)

	# --- 2. the split itself ------------------------------------------------------------------------------

	def test_a_known_vertical_narrows_a_name_search_to_that_vertical(self):
		"""A vertical is METADATA, never text — so today this query returns nothing at all. The split is what
		turns it into the right handful, which is the whole point of the phase."""
		query = f"{VERTICAL_A} {TOKEN}"
		self.assertEqual(self._leads(search_api.search(query)), set(), "the dormant path is expected to find nothing")
		self.assertEqual(self._leads(search_api.search(TOKEN)), {self.in_a, self.in_b}, "fixture: the name matches both")

		self._split(True)
		narrowed = search_api.search(query)
		self.assertEqual(self._leads(narrowed), {self.in_a}, "the recognised vertical did not narrow the result set")
		self.assertEqual(
			narrowed["understood"]["filters"],
			[{"column": "vertical", "label": frappe.get_meta("CRM Lead").get_field("custom_vertical").label, "value": VERTICAL_A}],
		)
		self.assertEqual(narrowed["understood"]["text"], TOKEN)

	def test_an_unknown_word_stays_text_and_returns_todays_results(self):
		self._split(True)
		response = search_api.search(TOKEN)
		self.assertNotIn("understood", response)
		self.assertEqual(self._leads(response), {self.in_a, self.in_b})

	# --- 3. two values for one column ---------------------------------------------------------------------

	def test_two_values_for_the_same_column_never_become_an_and_that_returns_nothing(self):
		"""`vertical = A AND vertical = B` is empty by construction. The first reading wins; the second value's
		words go back to the text lane, so the query degrades to a narrower text search, never to zero rows."""
		self._split(True)
		response = search_api.search(f"{VERTICAL_A} {VERTICAL_B} {TOKEN}")
		understood = response["understood"]
		self.assertEqual([f["column"] for f in understood["filters"]], ["vertical"], "a column was filtered twice")
		self.assertEqual(understood["filters"][0]["value"], VERTICAL_A, "the first reading did not win")
		for word in vocabulary.normalise(VERTICAL_B).split():
			self.assertIn(word, understood["text"], "the dropped value's words never reached the text lane")

	# --- 4. ambiguity is reported, never guessed -----------------------------------------------------------

	def test_an_ambiguous_term_produces_no_filter_and_its_words_are_searched(self):
		"""`BOTH` is a vertical AND a group. Picking one would be a guess; the endpoint must not make it."""
		self.assertGreater(len(vocabulary.terms()[vocabulary.normalise(BOTH)]), 1, "fixture: the term must be ambiguous")
		self._split(True)
		response = search_api.search(f"{BOTH} {TOKEN}")
		self.assertNotIn("understood", response, "an ambiguous term was resolved to a filter")
		self.assertEqual(self._leads(response), set(), "the ambiguous words did not reach the text lane")

	# --- 5. a filter narrows a text search; it never replaces one ------------------------------------------

	def test_a_query_with_nothing_left_for_text_is_answered_exactly_as_today(self):
		"""FTS5 has no match-everything: applying the filter with an empty query returns nothing, which would
		read as "understood, found nothing". Recorded as a limit, not papered over."""
		self._split(True)
		on = search_api.search(VERTICAL_A)
		self._split(False)
		off = search_api.search(VERTICAL_A)
		self.assertNotIn("understood", on)
		self.assertEqual(json.dumps(on, sort_keys=True, default=str), json.dumps(off, sort_keys=True, default=str))

	# --- 6. a client filter can never widen scope ----------------------------------------------------------

	def test_a_caller_supplied_permission_filter_cannot_widen_the_callers_own_scope(self):
		"""The framework merges permission filters LAST, so a hand-passed `principals` is overwritten. Asserted
		on the outcome: OTHER cannot reach the rep's leads by naming the rep's own principal token."""
		frappe.set_user(OTHER)
		try:
			engine = CRMLeadSearch()
			forged = engine.search(TOKEN, filters={"principals": ["LIKE", [f"|{REP}|"]]}) or {}
			self.assertEqual({r.get("lead") for r in forged.get("results", [])}, set())
		finally:
			frappe.set_user("Administrator")
		frappe.set_user(REP)
		try:
			mine = CRMLeadSearch().search(TOKEN) or {}
			self.assertEqual({r.get("lead") for r in mine.get("results", [])}, {self.in_a, self.in_b})
		finally:
			frappe.set_user("Administrator")

	def test_the_split_narrows_within_the_callers_scope_and_never_past_it(self):
		self._split(True)
		frappe.set_user(OTHER)
		try:
			self.assertEqual(self._leads(search_api.search(f"{VERTICAL_A} {TOKEN}")), set())
		finally:
			frappe.set_user("Administrator")
