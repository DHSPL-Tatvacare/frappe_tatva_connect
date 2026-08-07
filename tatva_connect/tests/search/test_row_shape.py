# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The row the spotlight draws: FOUR fixed slots, plus ONE dynamic slot for the ID the user actually typed.

    FIXED, always, in this order:  mobile · product line · group · program
    DYNAMIC, last, only when an ID was typed:  <Label> ▓<the whole ID, marked>▓

An identifier is **atomic** — the user either typed it or did not. So the SERVER decides which identifier the
query matched (a plain equality/prefix comparison on a short string; not a matcher, not a regex over content)
and returns it as its own field, whole. Because the whole value is returned, the row cannot render a broken
fragment of an ID, which is what the earlier snippet-and-highlight approach did.

What this suite locks:

  1. **No identifier reaches a displayed field.** The endpoint's own shape is the proof: patient id, prospect
     id and the alternate number are indexed metadata that the response NEVER carries — except in `ident`.
  2. **`ident` is decided, not guessed.** A patient id yields `ident`; the same lead found by NAME yields none.
  3. **The label is the field's own.** Read from `frappe.get_meta`, never a display string written in code.
  4. **A phone match marks the phone slot in place** — phone is already fixed, so it is never repeated.
  5. **`program` is in the index and filterable.** It was absent, so the fourth slot could not be drawn.

`test_indexed_columns.py` owns the other half (an identifier IS searchable, in `keys`); `test_result_ranking.py`
owns the order and the CRM Task removal.

No mocked index: the site's real index file is backed up, rebuilt for real against minted records, and
restored in cleanup — the DB transaction rolls back, a file does not.

Run:
    bench --site uatreplay.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_row_shape
"""
import os
import shutil
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.search import api as search_api
from tatva_connect.search import index as search_index
from tatva_connect.search.index import TOGGLE, CRMLeadSearch

TOKEN = "zzshapepatient"
PHONE_PREFIX = "+91610011"
PHONE = f"{PHONE_PREFIX}0001"
ALT_PHONE = f"{PHONE_PREFIX}0002"
EMAIL = "zzshape.patient@example.com"

# Every identifier that must never appear in a field the row displays.
HIDDEN = (ALT_PHONE, EMAIL)

# The fields the row really draws, in the order it draws them; `ident` is the one place an ID may appear.
DISPLAYED = ("title", "snippet", "status", "phone", "vertical", "group", "program", "assignee")


class TestRowShape(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		cls.vertical = frappe.get_all("CRM Vertical", pluck="name", order_by="name asc", limit=1)[0]
		cls.group = frappe.get_all("CRM Group", pluck="name", order_by="name asc", limit=1)[0]
		programs = frappe.get_all("CRM Program", pluck="name", order_by="name asc", limit=2)
		cls.program, cls.other_program = programs[0], programs[-1]
		cls.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": TOKEN, "last_name": "Shape", "status": "New",
			"mobile_no": PHONE, "email": EMAIL,
		}).insert(ignore_permissions=True).name
		# The grain axes are permlevel-1 fields, so they are written straight to the columns the index reads.
		for field, value in (("custom_vertical", cls.vertical), ("custom_group", cls.group), ("custom_current_program", cls.program)):
			frappe.db.set_value("CRM Lead", cls.lead, field, value, update_modified=False)
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
			frappe.db.delete("ToDo", {"reference_type": "CRM Lead", "reference_name": name})
			frappe.db.delete("DocShare", {"share_doctype": "CRM Lead", "share_name": name})
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1)
		frappe.db.set_value("CRM Tatva Automation", search_api.SPLIT_TOGGLE, "enabled", 0)
		engine = CRMLeadSearch()
		self.db_path = engine.db_path
		self.backup = f"{self.db_path}.shape-test.bak"
		if os.path.exists(self.db_path):
			shutil.copy2(self.db_path, self.backup)
		self.addCleanup(self._restore_site_index)
		engine.drop_index()
		engine.build_index()
		search_index.visible_principals.clear_cache()

	def _restore_site_index(self):
		if os.path.exists(self.backup):
			shutil.move(self.backup, self.db_path)
		elif os.path.exists(self.db_path):
			os.unlink(self.db_path)

	def _hit(self, query):
		return next(
			(h for h in search_api.search(query)["results"] if h["doctype"] == "CRM Lead" and h["lead"] == self.lead),
			None,
		)

	def _label(self, fieldname):
		return frappe.get_meta("CRM Lead").get_field(fieldname).label

	# --- 1. no identifier reaches a displayed field -----------------------------------------------------

	def test_a_lead_rows_snippet_is_empty_and_the_row_is_drawn_from_metadata(self):
		hit = self._hit(TOKEN)
		self.assertEqual(hit["snippet"], "", "a lead row carries a snippet again — ids will follow")
		self.assertEqual(hit["phone"], PHONE)
		self.assertEqual((hit["vertical"], hit["group"], hit["program"]), (self.vertical, self.group, self.program))

	def test_no_identifier_appears_in_any_displayed_field(self):
		"""Found by NAME: nothing was typed, so no ID may be anywhere in the response — not even as a key."""
		hit = self._hit(TOKEN)
		self.assertNotIn("ident", hit, "an ID was reported for a query that contained none")
		blob = " ".join(str(hit.get(field) or "") for field in DISPLAYED)
		for value in HIDDEN:
			self.assertNotIn(value, blob, f"{value!r} reached a displayed field")

	def test_the_response_never_carries_an_identifier_column_of_its_own(self):
		"""Structural, not textual: the shaped hit carries only `lead` and `phone` as identifier keys — every
		other identifier was removed. `lead` and `file_url` are navigation, and `ident` is the one slot."""
		hit = self._hit(TOKEN)
		for column, _fieldname, _kind in search_index._IDENTIFIERS:
			if column in ("lead", "phone"):
				continue
			self.assertNotIn(column, hit, f"{column} is an input-only identifier and must not be in the response")

	# --- 2. the dynamic slot: decided by the server, whole, labelled from meta --------------------------

	def test_typing_the_docname_returns_it_labelled_with_frappes_own_word_for_that_column(self):
		"""A docname is not a meta field, so it has no label of its own — and it matches whole, never by prefix."""
		hit = self._hit(self.lead)
		self.assertEqual(hit["ident"], {"column": "lead", "label": "ID", "value": self.lead})
		self.assertIsNone(search_index.matched_identifier({"lead": self.lead}, self.lead[:6]))

	# --- 3. a phone match marks the fixed slot in place -------------------------------------------------

	def test_a_phone_match_names_the_phone_slot_so_the_number_is_never_repeated(self):
		hit = self._hit(PHONE)
		self.assertEqual(hit["ident"], {"column": "phone", "label": self._label("mobile_no"), "value": PHONE})
		self.assertEqual(hit["ident"]["value"], hit["phone"], "the marked value must BE the fixed slot's value")

	def test_a_phone_typed_without_its_country_code_still_marks_the_stored_number(self):
		hit = self._hit(PHONE.lstrip("+")[-10:])
		self.assertEqual(hit["ident"]["column"], "phone")
		self.assertEqual(hit["ident"]["value"], PHONE, "the stored number, whole, is what the row marks")

	def test_an_email_is_not_indexed(self):
		hit = self._hit(EMAIL)
		self.assertIsNone(hit, "email is removed from indexing and must not find a lead")

	# --- 4. program: in the index, and filterable ------------------------------------------------------

	def test_program_is_a_metadata_column_and_narrows_a_search(self):
		self.assertIn("program", CRMLeadSearch.INDEX_SCHEMA["metadata_fields"])
		engine = CRMLeadSearch()
		leads = {r.get("lead") for r in (engine.search(TOKEN, filters={"program": self.program}) or {}).get("results", [])}
		self.assertIn(self.lead, leads, "the lead's own program did not match as a filter")
		if self.other_program != self.program:
			other = engine.search(TOKEN, filters={"program": self.other_program}) or {}
			self.assertNotIn(self.lead, {r.get("lead") for r in other.get("results", [])})

	def test_the_program_column_moved_the_schema_fingerprint(self):
		"""P0's guard is what lands a new metadata column on a site that already has an index."""
		engine = CRMLeadSearch()
		self.assertEqual(engine.stored_fingerprint(), engine.schema_fingerprint())
		current = engine.schema_fingerprint()
		without = {
			**CRMLeadSearch.INDEX_SCHEMA,
			"metadata_fields": [f for f in CRMLeadSearch.INDEX_SCHEMA["metadata_fields"] if f != "program"],
		}
		with patch.object(CRMLeadSearch, "INDEX_SCHEMA", without):
			self.assertNotEqual(current, CRMLeadSearch().schema_fingerprint())
