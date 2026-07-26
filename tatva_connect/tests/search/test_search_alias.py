# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An operator nickname must reach the matcher as the value it stands for — and keep reaching it after a rename.

The whole design turns on ONE decision: an alias stores a POINTER (target_doctype + target_name), never the value's
spelling. This app has already paid for the alternative — `merge_duplicate_grain_masters` had to hand-repair every
place a master's spelling had been stored as Data, because `rename_doc` cannot reach one. So the load-bearing test
here is `test_a_renamed_master_is_followed`: rename the master and the same typed word must resolve to the new value,
with nothing touched in between. Store the spelling instead and that test is the one that goes red.

The rest lock the four answers a save can give — resolved, unknown, held in two places, already a value in its own
right — because an alias that silently resolves to nothing is indistinguishable from search being broken.

Nothing is mocked: real masters, the real vocabulary, the real controller.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_search_alias
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.search import vocabulary

# Distinctive enough that no seeded master, and no patient, can already carry it.
LANE = "Aliaslane"
BOTH = "Aliasboth"
TERM = "altlane"


class TestSearchAlias(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(vocabulary.reload)
		vocabulary.reload()
		self.vertical = self._master("CRM Vertical", {"vertical_name": LANE})

	def _master(self, doctype, values):
		doc = frappe.get_doc({"doctype": doctype, **values}).insert()
		vocabulary.reload()
		return doc

	def _alias(self, term, resolves_to):
		return frappe.get_doc({"doctype": "CRM Search Alias", "term": term, "resolves_to": resolves_to}).insert()

	def test_a_typed_value_is_resolved_to_the_record_behind_it(self):
		# The operator types the value; the row comes back pointing at the record, and says which table it is in.
		alias = self._alias(TERM, LANE)
		self.assertEqual(alias.target_doctype, "CRM Vertical")
		self.assertEqual(alias.target_name, self.vertical.name)
		self.assertEqual(alias.resolves_to, LANE)
		# Stored the way a typed query is read, so the two can never be cut differently.
		self.assertEqual(alias.term, TERM)

	def test_the_alias_reaches_the_matcher_as_its_value(self):
		self._alias(TERM, LANE)
		reading = vocabulary.match(f"{TERM} kavita")
		self.assertIn(("vertical", LANE), reading.matched)
		self.assertEqual(reading.leftover, ["kavita"])

	def test_a_renamed_master_is_followed(self):
		# THE test. The pointer is what survives a rename; a stored spelling would leave the alias resolving to a
		# value that no longer exists, silently, with the row still looking correct on the form.
		self._alias(TERM, LANE)
		renamed = f"{LANE}-Renamed"
		frappe.rename_doc("CRM Vertical", self.vertical.name, renamed)
		vocabulary.reload()
		self.assertIn(("vertical", renamed), vocabulary.terms()[TERM])

	def test_an_unknown_value_is_refused_and_the_nearest_one_is_named(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			self._alias(TERM, LANE[:-1])
		self.assertIn(LANE, str(caught.exception))

	def test_a_value_held_in_two_places_is_refused(self):
		# One spelling, two masters: which one is meant is the operator's to settle, never the code's to guess.
		self._master("CRM Vertical", {"vertical_name": BOTH})
		self._master("CRM Group", {"group_name": BOTH})
		with self.assertRaises(frappe.ValidationError) as caught:
			self._alias("altboth", BOTH)
		self.assertIn("CRM Group", str(caught.exception))

	def test_a_word_that_is_already_a_value_is_refused(self):
		# Aliasing a real value to another one gives that word two meanings, and the matcher then narrows by neither.
		with self.assertRaises(frappe.ValidationError):
			self._alias(LANE, LANE)

	def test_a_term_below_the_floor_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._alias("a", LANE)

	def test_deleting_the_alias_takes_the_term_with_it(self):
		alias = self._alias(TERM, LANE)
		self.assertIn(TERM, vocabulary.terms())
		alias.delete()
		self.assertNotIn(TERM, vocabulary.terms())

	def test_the_vocabulary_is_dropped_as_the_row_is_written(self):
		# Without this the operator waits out the TTL and reads a correct alias as a broken search.
		self.assertNotIn(TERM, vocabulary.terms())
		self._alias(TERM, LANE)
		self.assertIn(TERM, vocabulary.terms())
