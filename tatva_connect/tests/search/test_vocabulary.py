# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The vocabulary must offer the strings the INDEX really holds — not the strings a catalogue says exist.

The trap this suite exists to catch: `prepare_document` writes a stage's own LABEL into `stage`, a User's
`full_name` into `assignee`, and a master's PK into `vertical` / `lead_group`. A vocabulary built from a
catalogue would hand P5 the composite stage PK and the owner's email, and the index would match neither —
silently, with no error and no empty-state. So the load-bearing test here is the one-brain lock: drive the real
`prepare_document` (and the real index file) and assert the vocabulary covers every value they produce.

It is a lock, not a snapshot: change the spelling in `index.py:_read_lead_context` — read a stage's key instead
of its label, index the owner's email instead of the full name, swap the master behind a column — and the lock
goes red. It went red for real once: the column was renamed `status` -> `stage` in the index and the vocabulary
kept naming `status`, which is not a column the index has, so the whole stage lane silently offered nothing.

No lead on this site carries a `custom_stage`, so one is minted here (written straight to the column the index
reads, since `custom_stage` is DERIVED from `custom_substage` by `lead/leads.py`) — otherwise the composite-key
path that owns the whole design would never be driven by data.

Run:
    bench --site uatreplay.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_vocabulary
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.search import vocabulary
from tatva_connect.search.index import CRMLeadSearch
from tatva_connect.taxonomy import labels

PHONE_PREFIX = "+91610007"
PHONE = f"{PHONE_PREFIX}0001"

# The closed metadata columns this module claims; the identifier columns are open sets and must never appear.
CLOSED = ("stage", "vertical", "lead_group", "program", "assignee")
OPEN = ("lead", "phone")


class TestVocabulary(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		cls.stage = frappe.get_all(
			"CRM Lead Stage", filters={"name": ["like", "%::%"]}, pluck="name", order_by="name asc", limit=1
		)[0]
		cls.vertical = frappe.get_all("CRM Vertical", pluck="name", order_by="name asc", limit=1)[0]
		cls.group = frappe.get_all("CRM Group", pluck="name", order_by="name asc", limit=1)[0]
		# An owner whose PK (an email) is NOT its full_name, so "the index stores the name, not the email" is testable.
		cls.owner = frappe.get_all(
			"User", filters={"name": ["like", "%@%"], "full_name": ["is", "set"]},
			pluck="name", order_by="name asc", limit=1,
		)[0]
		cls.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Vocabulary Lock", "mobile_no": PHONE, "status": "New",
			"custom_vertical": cls.vertical, "custom_group": cls.group,
		}).insert(ignore_permissions=True).name
		# Straight to the columns `_read_lead_context` reads: the doc hook derives custom_stage from custom_substage.
		frappe.db.set_value("CRM Lead", cls.lead, "custom_stage", cls.stage, update_modified=False)
		frappe.db.set_value("CRM Lead", cls.lead, "lead_owner", cls.owner, update_modified=False)
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
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(vocabulary._vocabulary.clear_cache)
		vocabulary._vocabulary.clear_cache()

	# --- helpers ----------------------------------------------------------------------------------------

	def _meanings_for(self, column):
		"""Every value the vocabulary offers for one column."""
		return {value for meanings in vocabulary.terms().values() for col, value in meanings if col == column}

	def _multi_word_term(self):
		"""A declared term of several words whose FIRST word is also a term of its own — the longest-match case."""
		for term, meanings in vocabulary.terms().items():
			if " " in term and term.split()[0] in vocabulary.terms() and len(meanings) == 1:
				return term
		return None

	# --- the one-brain lock -----------------------------------------------------------------------------

	def test_every_value_prepare_document_writes_is_in_the_vocabulary(self):
		"""Drive the REAL prepare_document over every lead on the site, including the minted stage-carrying one."""
		engine = CRMLeadSearch()
		offered = {column: self._meanings_for(column) for column in CLOSED}
		seen = {column: set() for column in CLOSED}

		for name in frappe.get_all("CRM Lead", pluck="name", limit_page_length=0):
			document = engine.prepare_document(frappe.get_doc("CRM Lead", name))
			if not document:
				continue
			for column in CLOSED:
				if document.get(column):
					seen[column].add(document[column])

		for column in CLOSED:
			missing = seen[column] - offered[column]
			self.assertEqual(missing, set(), f"prepare_document writes {column} values the vocabulary never offers")
			self.assertTrue(seen[column], f"no lead produced a {column} value — the lock proved nothing")

		# The minted lead is the only carrier of a composite stage; its LABEL, not its PK, is what was written.
		locked = engine.prepare_document(frappe.get_doc("CRM Lead", self.lead))
		self.assertEqual(locked["stage"], labels.stage_label(self.stage)[0])
		self.assertNotIn("::", locked["stage"])
		self.assertIn(("stage", locked["stage"]), vocabulary.terms()[vocabulary.normalise(locked["stage"])])

	def test_every_value_in_the_live_index_is_in_the_vocabulary(self):
		"""The same lock read straight out of the index FILE — what the framework actually stored."""
		engine = CRMLeadSearch()
		if not engine.is_search_enabled() or not engine.index_exists() or not engine._is_indexing_complete():
			self.skipTest("no complete index on this site to lock against")
		for column in CLOSED:
			rows = engine.sql(f"SELECT DISTINCT {column} AS value FROM search_fts", read_only=True)
			stored = {row["value"] for row in rows if row["value"]}
			self.assertTrue(stored, f"the index holds no {column} value — the lock proved nothing")
			self.assertEqual(stored - self._meanings_for(column), set(), f"the index holds unknown {column} values")

	# --- the stage label, and the PK that must NOT resolve ------------------------------------------------

	def test_a_stage_label_resolves_and_its_composite_pk_does_not(self):
		stages = frappe.get_all("CRM Lead Stage", filters={"name": ["like", "%::%"]}, pluck="name", order_by="name asc")
		labelled = {pk: labels.stage_label(pk)[0] for pk in stages}
		# One whose label carries exactly one meaning, so the assertion below is about the stage lane and not ambiguity.
		pk = next(
			p for p, label in labelled.items()
			if label and len(vocabulary.terms().get(vocabulary.normalise(label), ())) == 1
		)
		self.assertEqual(vocabulary.match(labelled[pk]).matched, [("stage", labelled[pk])])

		# The composite PK is what a catalogue-built vocabulary would offer, and the index would match none of it.
		self.assertNotIn(vocabulary.normalise(pk), vocabulary.terms())
		self.assertEqual([v for _, v in vocabulary.match(pk).matched if "::" in v], [])
		self.assertEqual([v for v in self._meanings_for("stage") if "::" in v], [],
		                 "a composite PK must never be offered as a stage value")

	def test_the_vocabulary_only_ever_names_a_column_the_index_declares(self):
		"""The defect this file missed: a column renamed in `index.py` leaves the vocabulary naming one that no
		longer exists, `_spellings` drops it on the `column in declared` guard, and the lane goes quiet — no
		error, no empty result, just a word that stops being understood."""
		declared = set(CRMLeadSearch.INDEX_SCHEMA["metadata_fields"])
		for column, _fieldname, _spelling in vocabulary._SOURCES:
			self.assertIn(column, declared, f"{column} is not a column the index declares")
		self.assertEqual(set(CLOSED), {column for column, _f, _s in vocabulary._SOURCES})

	# --- one column per meaning --------------------------------------------------------------------------

	def test_a_vertical_and_a_group_resolve_to_their_own_columns(self):
		verticals = self._meanings_for("vertical")
		groups = self._meanings_for("lead_group")
		vertical = sorted(v for v in verticals if v not in groups)[0]
		group = sorted(g for g in groups if g not in verticals)[0]

		self.assertEqual(vocabulary.match(vertical).matched, [("vertical", vertical)])
		self.assertEqual(vocabulary.match(group).matched, [("lead_group", group)])

	def test_an_assignee_is_the_full_name_the_index_stores_not_the_email(self):
		full_name = frappe.db.get_value("User", self.owner, "full_name")
		self.assertEqual(CRMLeadSearch().prepare_document(frappe.get_doc("CRM Lead", self.lead))["assignee"], full_name)
		self.assertIn(("assignee", full_name), vocabulary.terms()[vocabulary.normalise(full_name)])
		self.assertNotIn(vocabulary.normalise(self.owner), vocabulary.terms())

	def test_the_open_identifier_columns_are_absent(self):
		columns = {column for meanings in vocabulary.terms().values() for column, _ in meanings}
		self.assertEqual(columns - set(CLOSED), set())
		for column in OPEN:
			self.assertNotIn(column, columns, f"{column} is an open set and must stay in the full-text lane")

	# --- matching ------------------------------------------------------------------------------------------

	def test_a_hyphenated_value_matches_the_space_typed_form(self):
		stored = sorted(v for column in CLOSED for v in self._meanings_for(column) if "-" in v)
		self.assertTrue(stored, "no hyphenated value on this site — the fold proved nothing")
		for value in stored:
			typed = value.replace("-", " ")
			self.assertNotEqual(typed, value)
			result = vocabulary.match(typed)
			reached = [v for _, v in result.matched] + [v for _, meanings in result.ambiguous for _, v in meanings]
			self.assertIn(value, reached, f"a typed {typed!r} never met the stored {value!r}")

	def test_the_longest_phrase_wins_over_its_own_first_word(self):
		term = self._multi_word_term()
		self.assertIsNotNone(term, "no multi-word term shares its first word — the precedence proved nothing")
		result = vocabulary.match(term)
		self.assertEqual(result.matched, list(vocabulary.terms()[term]))
		self.assertEqual(result.leftover, [], "the shorter reading consumed part of the phrase")
		self.assertEqual(result.ambiguous, [])

	def test_an_unknown_word_is_leftover_not_dropped(self):
		result = vocabulary.match("zzunknownpatient")
		self.assertEqual(result.matched, [])
		self.assertEqual(result.leftover, ["zzunknownpatient"])

	def test_a_known_term_and_an_unknown_word_split_between_the_two_lanes(self):
		vertical = sorted(v for v in self._meanings_for("vertical") if v not in self._meanings_for("lead_group"))[0]
		result = vocabulary.match(f"{vertical} zzunknownpatient")
		self.assertEqual(result.matched, [("vertical", vertical)])
		self.assertEqual(result.leftover, ["zzunknownpatient"])

	# --- ambiguity -------------------------------------------------------------------------------------------

	def test_ambiguity_is_reported_never_resolved(self):
		ambiguous = sorted(term for term, meanings in vocabulary.terms().items() if len(meanings) > 1)
		if not ambiguous:
			self.skipTest("no term on this site carries two meanings")
		term = ambiguous[0]
		result = vocabulary.match(term)
		self.assertEqual(result.matched, [], f"{term!r} was silently resolved to one meaning")
		self.assertEqual(result.ambiguous, [(term, vocabulary.terms()[term])])
		self.assertEqual(result.leftover, term.split(), "an unresolved term must still reach the full-text lane")

	def test_the_same_query_gives_the_same_answer_twice(self):
		ambiguous = sorted(term for term, meanings in vocabulary.terms().items() if len(meanings) > 1)
		query = " ".join(filter(None, [ambiguous[0] if ambiguous else "", "zzunknownpatient"]))
		first = vocabulary.match(query)
		vocabulary._vocabulary.clear_cache()
		second = vocabulary.match(query)
		self.assertEqual(first, second)
		for meanings in vocabulary.terms().values():
			self.assertEqual(list(meanings), sorted(meanings), "meanings must carry a deterministic order")

	# --- the open-set cap --------------------------------------------------------------------------------------

	def test_a_master_over_the_cap_contributes_nothing(self):
		self.addCleanup(setattr, vocabulary, "MASTER_MAX", vocabulary.MASTER_MAX)
		vocabulary.MASTER_MAX = 0
		vocabulary._vocabulary.clear_cache()
		self.assertEqual(vocabulary.terms(), {}, "an over-cap master must be left to the full-text lane")

	# --- caching -------------------------------------------------------------------------------------------------

	def test_the_cache_is_one_site_wide_entry_not_one_per_user(self):
		"""The vocabulary is not an entitlement surface — one entry serves the site, and it carries an expiry."""
		frappe.set_user("Administrator")
		as_admin = vocabulary.terms()
		frappe.set_user("Guest")
		as_guest = vocabulary.terms()
		frappe.set_user("Administrator")

		self.assertEqual(as_admin, as_guest)
		keys = frappe.cache.get_keys(f"{vocabulary._vocabulary.__module__}.{vocabulary._vocabulary.__qualname__}")
		self.assertEqual(len(keys), 1, "a second session user must not mint a second vocabulary")
		self.assertNotIn(b"user:", keys[0], "the key must carry no user component")
		ttl = frappe.cache.ttl(keys[0])
		self.assertGreater(ttl, 0, "the vocabulary must expire, not live forever")
		self.assertLessEqual(ttl, vocabulary._TTL)
