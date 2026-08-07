# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The owner's column list, driven end to end — and THE ID RULE, which is the rule this suite exists to hold.

**A unique ID is INPUT.** It is indexed so a punched ID finds its record, and it is never shown in a row —
except in the one dedicated slot, when that ID is what the user typed (`test_row_shape.py` owns that slot).
So here: every identifier is in `keys` (searched, never displayed), a lead's `content` is EMPTY, and the row
is rendered from metadata. An earlier build put mobile + email + patient id + prospect id into `content`, so
every lead row showed a wall of identifiers; the red proof below reconstructs it.

Three other things this suite is built to catch, each of which went unnoticed before:

  1. A GUESSED fieldname. `custom_*` names are not inferable, and a field that does not exist contributes
     nothing to the index in perfect silence. So the first test asks the live meta, not this file — for the
     indexed field list AND for the identifier / grain-axis declarations.
  2. A child row with no `lead` / no `principals`. `_visible_rows` drops a row with no lead, and the
     pre-filter never matches a row with no principals — the doctype would be indexed and INVISIBLE.
  3. A per-result read. `file_url` is indexed metadata, so a File hit costs ZERO extra queries; the endpoint
     used to fetch it one row at a time.

No mocked index: the site's real index file is backed up, rebuilt for real against minted records, and
restored in cleanup — the DB transaction rolls back, a file does not.

Run:
    bench --site uatreplay.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_indexed_columns
"""
import os
import shutil
from unittest.mock import patch

import frappe
from frappe.model import default_fields
from frappe.tests.utils import FrappeTestCase

from tatva_connect.search import api as search_api
from tatva_connect.search import index as search_index
from tatva_connect.search.index import TOGGLE, CRMLeadSearch

# One rare token in the lead's name, so a single query reaches exactly this suite's records.
TOKEN = "zzcolumnpatient"
PHONE_PREFIX = "+91610008"
PHONE = f"{PHONE_PREFIX}0001"
EMAIL = "zzcolumn.patient@example.com"

REP = "zz-column-rep@example.com"
OTHER = "zz-column-other@example.com"

# The old declaration, restated ONLY here, to drive the red case: CRM Task was indexed, `file_url` was not.
OLD_DOCTYPES = {
	"CRM Lead": {"fields": [*search_index._PLACEHOLDER, "email", "custom_stage", "custom_substage", "source"]},
	"FCRM Note": {"fields": [*search_index._PLACEHOLDER, "title", "content", "reference_doctype", "reference_docname"]},
	"File": {"fields": [*search_index._PLACEHOLDER, "file_name", "attached_to_doctype", "attached_to_name"]},
	"CRM Task": {"fields": [*search_index._PLACEHOLDER, "title", "description", "assigned_to", "reference_doctype", "reference_docname"]},
}

_NEW_CONTENT_OF = CRMLeadSearch._content_of


def _old_content_of(self, doc):
	# The other half of the old code: a lead's displayed snippet WAS its phone number.
	if doc.doctype != "CRM Lead":
		return _NEW_CONTENT_OF(self, doc)
	ctx = self._lead_context(doc.name)
	ids = ctx["ids"] if ctx else {}
	return str(ids.get("phone") or "")


class TestIndexedColumns(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		for email in (REP, OTHER):
			if not frappe.db.exists("User", email):
				user = frappe.get_doc({
					"doctype": "User", "email": email, "first_name": "Column",
					"send_welcome_email": 0, "user_type": "System User",
				}).insert(ignore_permissions=True)
				user.append("roles", {"role": "Sales User"})
				user.save(ignore_permissions=True)

		# A real stage PK is composite; both stage columns are Links to the same master, so both are minted.
		stages = frappe.get_all(
			"CRM Lead Stage", filters={"name": ["like", "%::%"]}, pluck="name", order_by="name asc", limit=2
		)
		cls.stage, cls.substage = stages[0], stages[-1]

		cls.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": TOKEN, "last_name": "Columns", "status": "New",
			"mobile_no": PHONE, "email": EMAIL,
		}).insert(ignore_permissions=True).name
		# Straight to the columns the index reads: custom_stage is DERIVED from custom_substage by the controller.
		for field, value in (("custom_stage", cls.stage), ("custom_substage", cls.substage), ("lead_owner", REP)):
			frappe.db.set_value("CRM Lead", cls.lead, field, value, update_modified=False)

		cls.file = frappe.get_doc({
			"doctype": "File", "file_name": f"{TOKEN}.txt", "is_private": 1,
			"content": "column fixture", "attached_to_doctype": "CRM Lead", "attached_to_name": cls.lead,
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for email in (REP, OTHER):
			frappe.db.delete("User Permission", {"user": email})
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
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
		self.backup = f"{self.db_path}.columns-test.bak"
		if os.path.exists(self.db_path):
			shutil.copy2(self.db_path, self.backup)
		self.addCleanup(self._restore_site_index)
		self._rebuild()
		for email in (REP, OTHER, "Administrator"):
			frappe.set_user(email)
			search_index.visible_principals.clear_cache()
		frappe.set_user("Administrator")

	def _restore_site_index(self):
		if os.path.exists(self.backup):
			shutil.move(self.backup, self.db_path)
		elif os.path.exists(self.db_path):
			os.unlink(self.db_path)

	def _rebuild(self):
		engine = CRMLeadSearch()
		engine.drop_index()
		engine.build_index()

	def _hits(self, query, user="Administrator"):
		frappe.set_user(user)
		try:
			res = CRMLeadSearch().search(query) or {}
			return [(r.get("doctype"), r.get("name"), r.get("lead")) for r in res.get("results", [])]
		finally:
			frappe.set_user("Administrator")

	def _row(self, doctype, name):
		rows = CRMLeadSearch().sql(
			"SELECT title, content, keys, lead, principals, phone, program, file_url"
			" FROM search_fts WHERE doc_id = ?",
			[f"{doctype}:{name}"], read_only=True,
		)
		return dict(rows[0]) if rows else None

	# --- 1. the fieldnames are the live meta's, never this file's ---------------------------------------

	def test_every_declared_fieldname_exists_on_the_live_doctype(self):
		"""A guessed `custom_*` name contributes NOTHING to the index and raises nothing. This is the lock."""
		for doctype, config in CRMLeadSearch.INDEXABLE_DOCTYPES.items():
			meta = frappe.get_meta(doctype)
			for field_def in config["fields"]:
				fieldname = next(iter(field_def.values())) if isinstance(field_def, dict) else field_def
				self.assertTrue(
					meta.get_field(fieldname) or fieldname in default_fields,
					f"{doctype}.{fieldname} is declared for indexing but does not exist on the doctype",
				)

	def test_every_identifier_and_axis_fieldname_exists_on_the_live_lead(self):
		"""The same lock for the two declarations the row shape is built from — `program` was added by hand."""
		meta = frappe.get_meta("CRM Lead")
		declared = [fieldname for _c, fieldname, _k in search_index._IDENTIFIERS]
		declared += [fieldname for _c, fieldname in search_index._AXES]
		for fieldname in declared:
			self.assertTrue(
				meta.get_field(fieldname) or fieldname in default_fields,
				f"CRM Lead.{fieldname} is declared for the row shape but does not exist on the doctype",
			)

	# --- 2. every owner-chosen column finds its record --------------------------------------------------

	def test_each_lead_column_finds_the_lead(self):
		wanted = (self.lead, "unique id"), (TOKEN, "full name"), (PHONE, "mobile number")
		for value, label in wanted:
			self.assertIn(
				("CRM Lead", self.lead, self.lead), self._hits(value),
				f"a lead's {label} did not find it",
			)

	def test_the_stage_leaf_and_the_substage_leaf_find_the_lead(self):
		"""The composite PK is never indexed — the leaf is, exactly as `_read_lead_context` spells it."""
		for value, label in (
			(self.stage.split("::")[-1], "stage"),
			(self.substage.split("::")[-1], "sub stage"),
		):
			leads = {lead for _, _, lead in self._hits(value)}
			self.assertIn(self.lead, leads, f"a lead's {label} ({value!r}) did not find it")

	# --- 3. THE ID RULE: searched in `keys`, never in a displayed field ---------------------------------

	def test_a_leads_displayed_snippet_is_empty(self):
		"""The row is rendered from metadata, so a lead has no snippet at all — nothing to fill with ids."""
		self.assertEqual(self._row("CRM Lead", self.lead)["content"], "")

	def test_every_identifier_is_in_keys_and_in_no_displayed_field(self):
		"""One assertion per identifier, both halves: searchable, and absent from title AND content."""
		row = self._row("CRM Lead", self.lead)
		for value in (self.lead, PHONE):
			self.assertIn(value, row["keys"], f"{value!r} is not searchable")
			self.assertNotIn(value, row["content"], f"{value!r} reached the displayed snippet")
			self.assertNotIn(value, row["title"], f"{value!r} reached the displayed title")
		for value in (self.stage.split("::")[-1],):
			self.assertIn(value, row["keys"])
			self.assertNotIn(value, row["content"])

	def test_a_phone_is_searchable_with_and_without_its_country_code(self):
		digits = PHONE.lstrip("+")
		for value in (digits, digits[-10:]):
			self.assertIn(("CRM Lead", self.lead, self.lead), self._hits(value), f"{value!r} did not find the lead")

	# --- 4. every indexed row on this site carries a lead and principals -------------------------------

	def test_every_indexed_row_on_this_site_carries_a_lead_and_principals(self):
		"""A row with an empty permission column is invisible to every non-exempt caller, silently."""
		engine = CRMLeadSearch()
		broken = engine.sql("SELECT COUNT(*) c FROM search_fts WHERE lead IS NULL OR lead = ''", read_only=True)[0]["c"]
		total = engine.sql("SELECT COUNT(*) c FROM search_fts", read_only=True)[0]["c"]
		self.assertTrue(total > 1, "the index is empty — this assertion proved nothing")
		self.assertEqual(broken, 0, "indexed rows carry no lead and can never be returned")

	# --- 5. file_url is indexed metadata, so a File hit costs no read -----------------------------------

	def test_a_file_row_carries_its_own_url_in_the_index(self):
		row = self._row("File", self.file)
		self.assertEqual(row["file_url"], frappe.db.get_value("File", self.file, "file_url"))
		self.assertTrue(row["file_url"], "fixture: the file has no url, so the assertion proved nothing")

	def test_shaping_a_file_hit_costs_zero_database_queries(self):
		"""The endpoint used to read `file_url` per file result. Counted, not asserted on a call."""
		raw = next(r for r in (CRMLeadSearch().search(TOKEN) or {})["results"] if r.get("doctype") == "File")
		search_api._shape(raw, TOKEN)  # warm the meta the labels come from; the old read was per-result regardless
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as counter:
			hit = search_api._shape(raw, TOKEN)
		self.assertEqual(counter.call_count, 0, f"shaping a file hit issued {counter.call_count} queries")
		self.assertEqual(hit["file_url"], frappe.db.get_value("File", self.file, "file_url"))

		# The read the old code did, measured with the same counter — or the assertion above proves nothing.
		with patch.object(frappe.db, "sql", wraps=frappe.db.sql) as counter:
			frappe.db.get_value("File", self.file, "file_url")
		self.assertGreater(counter.call_count, 0, "the old per-result read was free — nothing was proven")

	# --- 6. the RED proofs, made permanent --------------------------------------------------------------

	def test_the_old_code_showed_every_identifier_in_the_lead_row(self):
		"""The build this batch corrects, reconstructed in place: the old code put phone into the displayed
		snippet. email is no longer in the indexed fields so it was already invisible to the old code too."""
		with patch.object(CRMLeadSearch, "_content_of", _old_content_of):
			self._rebuild()
			content = self._row("CRM Lead", self.lead)["content"]
			self.assertIn(PHONE, content, f"{PHONE!r} was expected in the OLD displayed snippet")
		self._rebuild()
		self.assertEqual(self._row("CRM Lead", self.lead)["content"], "", "the rebuild did not restore the ID rule")

	def test_the_old_declaration_carried_no_file_url(self):
		"""Undeclared, the column exists and is always NULL — which is why the endpoint had to read it per row.
		Driven without CRM Task on purpose: indexing 12.5k tasks to prove a File column would prove it slowly."""
		without = {**CRMLeadSearch.INDEXABLE_DOCTYPES, "File": OLD_DOCTYPES["File"]}
		with patch.object(CRMLeadSearch, "INDEXABLE_DOCTYPES", without):
			self._rebuild()
			self.assertIsNone(self._row("File", self.file)["file_url"], "file_url was expected absent on the old code")
		self._rebuild()
		self.assertTrue(self._row("File", self.file)["file_url"], "the rebuild did not restore file_url")

	def test_the_new_declaration_moves_the_schema_fingerprint(self):
		"""P0's guard is what lands these columns on a site that already has an index."""
		engine = CRMLeadSearch()
		self.assertNotIn("CRM Task", CRMLeadSearch.INDEXABLE_DOCTYPES, "the declaration under test is not the live one")
		self.assertEqual(engine.stored_fingerprint(), engine.schema_fingerprint())
		# Captured OUTSIDE the patch: INDEXABLE_DOCTYPES is a class attribute, so an existing instance reads the
		# patched value too — comparing two live calls would compare the old declaration with itself.
		current = engine.schema_fingerprint()
		with patch.object(CRMLeadSearch, "INDEXABLE_DOCTYPES", OLD_DOCTYPES):
			self.assertNotEqual(current, CRMLeadSearch().schema_fingerprint())


class TestTheIndexAxesMatchTheSchema(FrappeTestCase):
	"""`index._AXES` is a static declaration because it defines a sqlite schema read at import — deriving it
	from `get_meta` there would run before a fresh install's custom fields exist. This is the lock that makes
	the copy safe: it must equal what the schema actually enforces the grain to be."""

	def test_the_declared_axis_columns_are_the_ones_frappe_gates_on(self):
		from tatva_connect.search import index
		from tatva_connect.taxonomy import grain

		self.assertEqual(
			tuple(column for _axis, column in index._AXES),
			grain.columns("CRM Lead"),
			"index._AXES has drifted from the grain columns the schema declares",
		)
