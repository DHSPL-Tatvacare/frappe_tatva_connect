# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Anything searchable is either on the row or deliberately typed.

THE RULE. A hit a reader cannot explain is noise. Every term the index matches on must therefore be one of
two things: text the row DISPLAYS, so a match is self-evident; or an identifier a person PUNCHES IN — a
docname, a patient ID, a phone in any of its digit forms — which nobody types by accident and which
`matched_identifier` names back on the response.

WHAT IT COST TO LEARN. The owner's EMAIL was indexed beside the owner's name. It is displayed nowhere and
nobody searching for a patient types one, so `crm` reached 861 leads through `crm.user1@example.com` and
every hit looked like a bug: twenty patients, no highlight, nothing on screen that said `crm`.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_searchable_is_visible
"""
import unittest

import frappe

from tatva_connect.search import api as search_api
from tatva_connect.search.index import CRMLeadSearch

# The columns FTS matches on. Anything outside these is stored and never searched.
_SEARCHABLE = ("title", "content", "keys")


class SearchableCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.engine = CRMLeadSearch()

	def a_row(self, doctype, filters=None):
		name = frappe.db.get_value(doctype, filters or {}, "name")
		if not name:
			self.skipTest(f"no {doctype} on this bench")
		return self.engine.prepare_document(frappe.get_doc(doctype, name))


class TestNoSearchableColumnCarriesAnEmail(SearchableCase):
	"""The sharp edge of the rule, and the one that was actually broken."""

	def test_a_lead_row_matches_on_no_email(self):
		row = self.a_row("CRM Lead")
		if not row:
			self.skipTest("this lead resolves to no indexable row")
		for column in _SEARCHABLE:
			self.assertNotIn(
				"@", row.get(column) or "",
				f"`{column}` carries an email — nothing displays one and nobody types one to find a patient",
			)

	def test_a_file_row_matches_on_no_email(self):
		row = self.a_row("File", {"attached_to_doctype": "CRM Lead"})
		if not row:
			self.skipTest("no File on this bench hangs off a lead")
		for column in _SEARCHABLE:
			self.assertNotIn("@", row.get(column) or "", f"`{column}` carries an email")


class TestAFileIsFoundByItsOwnText(SearchableCase):
	"""A file is a record with a name; it is not an attribute of its patient."""

	def test_its_title_is_its_filename_not_the_patient(self):
		name, file_name = (frappe.db.get_value(
			"File", {"attached_to_doctype": "CRM Lead", "file_name": ("is", "set")},
			["name", "file_name"]) or (None, None))
		if not name:
			self.skipTest("no named File on this bench hangs off a lead")
		row = self.engine.prepare_document(frappe.get_doc("File", name))
		if not row:
			self.skipTest("that File resolves to no indexable row")
		self.assertEqual(row["title"], file_name)
		# The patient rides along to be SHOWN, and is not one of the searchable columns.
		self.assertTrue(row.get("lead_name"))
		for column in _SEARCHABLE:
			self.assertNotIn(row["lead_name"], row.get(column) or "",
			                 f"the patient's name is searchable through `{column}`, so a lead match drags "
			                 f"in every file of that patient")


class TestTheFloorMeasuresTheTerm(unittest.TestCase):
	"""A minimum on the raw string is no minimum: punctuation walks a short term straight past it."""

	def test_punctuation_does_not_smuggle_a_short_term_through(self):
		self.assertEqual(search_api._searchable("crm/"), "crm")
		self.assertEqual(search_api._searchable("ab-"), "ab")
		self.assertEqual(search_api._searchable("x.y"), "xy")

	def test_a_real_term_is_left_alone(self):
		self.assertEqual(search_api._searchable("discharge"), "discharge")
		self.assertEqual(search_api._searchable("+919812345678"), "919812345678")

	def test_the_two_spellings_of_one_short_query_agree(self):
		engine = CRMLeadSearch()
		if not engine.is_search_enabled():
			self.skipTest("search is dormant on this bench")
		self.assertEqual(search_api._status(engine, "crm"), search_api._status(engine, "crm/"))
