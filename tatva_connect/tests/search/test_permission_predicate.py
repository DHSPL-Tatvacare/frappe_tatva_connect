# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""`get_search_filters()` used to return `{"lead": <every lead the caller may see>}`. The framework binds ONE
SQL variable per id (`frappe/search/sqlite_search.py:895`), so the statement grew with the lead table. Past
the connection's `SQLITE_MAX_VARIABLE_NUMBER` sqlite3 raises `too many SQL variables`, and `search()` SWALLOWS
it (`sqlite_search.py:268-272`: log, `raw_results = []`). The users with the WIDEST visibility — managers —
therefore got silently zero results, while a rep's search worked.

The fix is two bounded layers, and this suite asserts both separately:

  1. a PRE-filter bounded by HEADCOUNT — `(principals LIKE ? OR ...)`, one bound variable per PRINCIPAL, over a
     denormalised column holding the lead's owner/creator/assignee/share set. `sqlite_search.py:811-820` is
     the framework's own OR-of-LIKEs seam; nothing is hand-rolled.
  2. an AUTHORITATIVE post-filter bounded by RESULT COUNT — the candidate leads are handed to `get_list`, the
     same brain the enumeration used, as ONE bounded question. This is what enforces grain User Permissions,
     which the index carries no column for. Without it the fix would WIDEN visibility, and
     `test_a_lead_outside_the_reps_grain_is_never_returned_even_though_they_own_it` is the lock on that.

The cliff is driven for real, without seeding 33k leads: `_get_connection` is wrapped in the TEST ONLY to
lower `SQLITE_LIMIT_VARIABLE_NUMBER` on the connection the framework itself opens, so the genuine
`sqlite3.OperationalError` fires and is genuinely swallowed. Asserting a parameter count would have proved
the shape of the statement; this proves the user-visible outcome.

No mocked index: the site's real index file is backed up, rebuilt for real against minted records, and
restored in cleanup — the DB transaction rolls back, a file does not.

Run:
    bench --site uatreplay.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.search.test_permission_predicate
"""
import os
import shutil
import sqlite3
from unittest.mock import patch

import frappe
from frappe.search.sqlite_search import SQLiteSearch
from frappe.tests.utils import FrappeTestCase

from tatva_connect.search import index as search_index
from tatva_connect.search.index import TOGGLE, CRMLeadSearch

VERTICAL_A = "ZZ Predicate A"
VERTICAL_B = "ZZ Predicate B"
GROUP = "ZZ Predicate Group"
REP = "zz-predicate-rep@example.com"
MGR = "zz-predicate-mgr@example.com"
OTHER = "zz-predicate-other@example.com"

# One rare token every minted lead's name carries, so a single query matches exactly this suite's records.
TOKEN = "zzpredicatepatient"
PHONE_PREFIX = "+91610007"

REP_OWNED_IN_A = 10
OTHER_OWNED_IN_A = 4
REP_OWNED_IN_B = 3

# Below every persona's visible-lead count and above the handful the predicate needs — the cliff, in miniature.
LOWERED_VARIABLE_LIMIT = 8


class TestPermissionPredicate(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		for vertical in (VERTICAL_A, VERTICAL_B):
			if not frappe.db.exists("CRM Vertical", vertical):
				frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": vertical}).insert(ignore_permissions=True)
		if not frappe.db.exists("CRM Group", GROUP):
			frappe.get_doc({"doctype": "CRM Group", "group_name": GROUP}).insert(ignore_permissions=True)

		# REP is a plain Sales User: org_hierarchy narrows them to owned + assigned, so the PRE-filter applies.
		# MGR is a Sales Manager outside the hierarchy: org_hierarchy narrows them by NOTHING, which is the
		# exemption the old code missed — it asked grain entitlement instead and enumerated them anyway.
		cls._user(REP, ["Sales User"], VERTICAL_A)
		cls._user(MGR, ["Sales Manager"], VERTICAL_A)
		cls._user(OTHER, ["Sales User"], VERTICAL_B)

		cls.rep_in_a = [cls._lead(VERTICAL_A, REP) for _ in range(REP_OWNED_IN_A)]
		cls.other_in_a = [cls._lead(VERTICAL_A, OTHER) for _ in range(OTHER_OWNED_IN_A)]
		cls.rep_in_b = [cls._lead(VERTICAL_B, REP) for _ in range(REP_OWNED_IN_B)]
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for email in (REP, MGR, OTHER):
			frappe.db.delete("User Permission", {"user": email})
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		for dt, name in (("CRM Group", GROUP), ("CRM Vertical", VERTICAL_A), ("CRM Vertical", VERTICAL_B)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"{PHONE_PREFIX}%"]}, pluck="name"):
			frappe.db.delete("ToDo", {"reference_type": "CRM Lead", "reference_name": name})
			frappe.db.delete("DocShare", {"share_doctype": "CRM Lead", "share_name": name})
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	@classmethod
	def _user(cls, email, roles, vertical):
		if not frappe.db.exists("User", email):
			user = frappe.get_doc({
				"doctype": "User", "email": email, "first_name": "Predicate",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
			for role in roles:
				user.append("roles", {"role": role})
			user.save(ignore_permissions=True)
		if not frappe.db.exists("User Permission", {"user": email, "allow": "CRM Vertical"}):
			frappe.get_doc({
				"doctype": "User Permission", "user": email,
				"allow": "CRM Vertical", "for_value": vertical, "applicable_for": "CRM Lead",
			}).insert(ignore_permissions=True)

	_seq = 0

	@classmethod
	def _lead(cls, vertical, owner):
		# lead_owner is stamped AFTER the insert, deliberately. Passing it in makes crm's own controller
		# (`crm_lead.py:189`) share the lead with that agent, and a DocShare is OR-ed in by `DatabaseQuery`
		# OUTSIDE the user-permission match — so an owner set the normal way is visible regardless of grain
		# and the fixture could not express "a principal the grain still refuses". Sharing is exercised
		# explicitly in its own test instead.
		cls._seq += 1
		doc = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": TOKEN, "last_name": f"N{cls._seq}",
			"mobile_no": f"{PHONE_PREFIX}{cls._seq:04d}", "status": "New",
			"custom_vertical": vertical, "custom_group": GROUP,
		}).insert(ignore_permissions=True)
		frappe.db.set_value("CRM Lead", doc.name, "lead_owner", owner, update_modified=False)
		return doc.name

	# --- harness ------------------------------------------------------------------------------------

	def setUp(self):
		self.addCleanup(frappe.set_user, "Administrator")
		# Saving a CRM Lead commits (notifications, enqueues), so a mutating test can outlive the rollback.
		# Every test therefore starts from the declared fixture state, restated here, and is order-independent.
		self._restate_fixture()
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1)
		engine = CRMLeadSearch()
		self.db_path = engine.db_path
		self.backup = f"{self.db_path}.predicate-test.bak"
		if os.path.exists(self.db_path):
			shutil.copy2(self.db_path, self.backup)
		self.addCleanup(self._restore_site_index)
		engine.drop_index()
		engine.build_index()
		for email in (REP, MGR, OTHER, "Administrator"):
			self._clear_cache_as(email)

	def _restate_fixture(self):
		for leads, owner in ((self.rep_in_a, REP), (self.other_in_a, OTHER), (self.rep_in_b, REP)):
			for lead in leads:
				frappe.db.set_value("CRM Lead", lead, "lead_owner", owner, update_modified=False)
				frappe.db.delete("ToDo", {"reference_type": "CRM Lead", "reference_name": lead})
				frappe.db.delete("DocShare", {"share_doctype": "CRM Lead", "share_name": lead})
		frappe.db.commit()

	def _restore_site_index(self):
		if os.path.exists(self.backup):
			shutil.move(self.backup, self.db_path)
		elif os.path.exists(self.db_path):
			os.unlink(self.db_path)

	def _clear_cache_as(self, email):
		frappe.set_user(email)
		search_index.visible_principals.clear_cache()
		frappe.set_user("Administrator")

	def _leads_found_by(self, email, variable_limit=None):
		"""The lead ids a persona's own search really returns, through the real engine and index."""
		frappe.set_user(email)
		try:
			engine = CRMLeadSearch()
			if variable_limit:
				engine._get_connection = self._limited_connection(engine, variable_limit)
			res = engine.search(TOKEN) or {}
			return {r.get("lead") for r in res.get("results", [])}, res.get("summary", {})
		finally:
			frappe.set_user("Administrator")

	def _limited_connection(self, engine, limit):
		"""The framework's own connection, with the runtime's variable ceiling lowered on it — the ONLY thing
		this changes is the number of bound variables sqlite will accept, which is exactly the cliff."""
		real = type(engine)._get_connection

		def limited(read_only=False):
			conn = real(engine, read_only=read_only)
			conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, limit)
			return conn

		return limited

	def _filters_as(self, email):
		frappe.set_user(email)
		try:
			return CRMLeadSearch().get_search_filters()
		finally:
			frappe.set_user("Administrator")

	def _principals_column(self, lead):
		rows = CRMLeadSearch().sql(
			"SELECT principals FROM search_fts WHERE doc_id = ?", [f"CRM Lead:{lead}"], read_only=True
		)
		return rows[0]["principals"] if rows else None

	# --- 1. the cliff -------------------------------------------------------------------------------

	def test_search_survives_a_variable_ceiling_the_enumeration_blew_straight_through(self):
		"""RED on the old code: 14 (MGR) and 10 (REP) bound lead ids against a ceiling of 8 raise
		`too many SQL variables`, the framework swallows it, and both personas get an empty list."""
		rep_found, _ = self._leads_found_by(REP, variable_limit=LOWERED_VARIABLE_LIMIT)
		mgr_found, _ = self._leads_found_by(MGR, variable_limit=LOWERED_VARIABLE_LIMIT)
		self.assertEqual(rep_found, set(self.rep_in_a), "the rep's search died on the variable ceiling")
		self.assertEqual(
			mgr_found, set(self.rep_in_a) | set(self.other_in_a),
			"the manager's search died on the variable ceiling — the exact silent-zero-results defect",
		)

	def test_no_lead_id_is_ever_bound_into_the_statement(self):
		"""The shape behind the outcome above: the filter names PRINCIPALS, never leads."""
		filters = self._filters_as(REP)
		self.assertEqual(list(filters), ["principals"])
		operator, values = filters["principals"]
		self.assertEqual(operator, "LIKE")
		# `|everyone|` is the DocShare-to-everyone grant, which reaches every logged-in caller.
		self.assertEqual(values, ["|everyone|", f"|{REP}|"])
		every_lead = set(self.rep_in_a) | set(self.other_in_a) | set(self.rep_in_b)
		self.assertEqual(every_lead & set(values), set())

	def test_the_bound_variable_count_does_not_grow_with_the_lead_table(self):
		"""The invariant, asserted by construction: five more visible leads, the same number of variables."""
		before = len(self._filters_as(REP)["principals"][1])
		extra = [self._lead(VERTICAL_A, REP) for _ in range(5)]
		self.addCleanup(lambda: [frappe.delete_doc("CRM Lead", n, force=True, ignore_permissions=True) for n in extra])
		self._clear_cache_as(REP)
		self.assertEqual(len(self._filters_as(REP)["principals"][1]), before)

	# --- 2. the security assertion ------------------------------------------------------------------

	def test_a_manager_and_a_rep_get_correct_and_different_result_sets(self):
		rep_found, _ = self._leads_found_by(REP)
		mgr_found, _ = self._leads_found_by(MGR)
		self.assertEqual(rep_found, set(self.rep_in_a))
		self.assertEqual(mgr_found, set(self.rep_in_a) | set(self.other_in_a))
		self.assertNotEqual(rep_found, mgr_found, "the fix must not have widened the rep to the manager's view")
		for lead in self.other_in_a:
			self.assertNotIn(lead, rep_found, "the rep was shown a lead they neither own nor are assigned")

	def test_a_lead_outside_the_reps_grain_is_never_returned_even_though_they_own_it(self):
		"""The anti-widening lock. The PRE-filter passes these rows — the rep IS their principal — so the only
		thing that keeps them out is the authoritative `get_list` gate. Delete that gate and this goes red."""
		rep_found, _ = self._leads_found_by(REP)
		mgr_found, _ = self._leads_found_by(MGR)
		for lead in self.rep_in_b:
			self.assertIn(f"|{REP}|", self._principals_column(lead) or "", "fixture: the rep must be a principal")
			self.assertNotIn(lead, rep_found, "a lead outside the rep's entitled grain was returned")
			self.assertNotIn(lead, mgr_found)

	def test_the_reported_total_never_counts_a_lead_the_caller_cannot_see(self):
		_, summary = self._leads_found_by(REP)
		self.assertEqual(summary.get("total_matches"), REP_OWNED_IN_A)

	# --- 3. the exemption ---------------------------------------------------------------------------

	def test_a_sales_manager_outside_the_hierarchy_searches_without_any_enumeration(self):
		"""RED on the old code: it asked grain entitlement, so this persona (not ALL_GRAINS) was enumerated
		in full. The list engine's own answer is that org_hierarchy narrows them by nothing."""
		self.assertEqual(self._filters_as(MGR), {})
		mgr_found, _ = self._leads_found_by(MGR)
		self.assertEqual(mgr_found, set(self.rep_in_a) | set(self.other_in_a))

	def test_administrator_is_exempt_and_still_scoped_by_the_authoritative_gate(self):
		self.assertEqual(self._filters_as("Administrator"), {})
		found, _ = self._leads_found_by("Administrator")
		self.assertEqual(found, set(self.rep_in_a) | set(self.other_in_a) | set(self.rep_in_b))

	# --- 4. reindex ---------------------------------------------------------------------------------

	def test_changing_the_owner_makes_the_lead_appear_for_the_new_owner_and_a_handover_takes_it_away(self):
		lead = self.other_in_a[0]
		self.assertNotIn(lead, self._leads_found_by(REP)[0])
		doc = frappe.get_doc("CRM Lead", lead)
		doc.lead_owner = REP
		doc.save(ignore_permissions=True)
		self.assertIn(f"|{REP}|", self._principals_column(lead) or "")
		self.assertIn(lead, self._leads_found_by(REP)[0], "a reassigned lead stayed invisible to its new owner")

		# crm's own controller ASSIGNS and SHARES the lead with each new owner (`crm_lead.assign_agent` /
		# `share_with_agent`) and never un-assigns the previous one, so moving `lead_owner` back is not a
		# handover on its own. All three legs land in the same column, and it must lose the user on the last.
		doc = frappe.get_doc("CRM Lead", lead)
		doc.lead_owner = OTHER
		doc.save(ignore_permissions=True)
		self._detach(lead, REP)
		self.assertNotIn(f"|{REP}|", self._principals_column(lead) or "")
		self.assertNotIn(lead, self._leads_found_by(REP)[0], "a handed-over lead stayed visible to the old owner")

	def _detach(self, lead, user):
		for name in frappe.get_all(
			"ToDo",
			filters={"reference_type": "CRM Lead", "reference_name": lead, "allocated_to": user, "status": ["!=", "Cancelled"]},
			pluck="name",
		):
			todo = frappe.get_doc("ToDo", name)
			todo.status = "Cancelled"
			todo.save(ignore_permissions=True)
		if frappe.db.exists("DocShare", {"share_doctype": "CRM Lead", "share_name": lead, "user": user}):
			frappe.share.remove(
				"CRM Lead", lead, user, flags={"ignore_share_permission": True, "ignore_permissions": True}
			)

	def test_an_assignment_makes_the_lead_appear_and_cancelling_it_takes_it_away(self):
		lead = self.other_in_a[1]
		self.assertNotIn(lead, self._leads_found_by(REP)[0])
		todo = frappe.get_doc({
			"doctype": "ToDo", "reference_type": "CRM Lead", "reference_name": lead,
			"allocated_to": REP, "status": "Open", "description": "predicate assignment",
		}).insert(ignore_permissions=True)
		self.addCleanup(frappe.db.delete, "ToDo", {"name": todo.name})
		self.assertIn(f"|{REP}|", self._principals_column(lead) or "")
		self.assertIn(lead, self._leads_found_by(REP)[0], "an assigned lead was unfindable by its assignee")

		todo.reload()
		todo.status = "Cancelled"
		todo.save(ignore_permissions=True)
		self.assertNotIn(f"|{REP}|", self._principals_column(lead) or "")
		self.assertNotIn(lead, self._leads_found_by(REP)[0], "a cancelled assignment still granted visibility")

	def test_a_child_row_follows_its_parent_leads_ownership(self):
		lead = self.other_in_a[2]
		note = frappe.get_doc({
			"doctype": "FCRM Note", "title": f"{TOKEN} note", "content": "predicate child row",
			"reference_doctype": "CRM Lead", "reference_docname": lead,
		}).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "FCRM Note", note.name, force=True, ignore_permissions=True)
		CRMLeadSearch().index_doc("FCRM Note", note.name)
		self.assertNotIn(f"|{REP}|", self._note_principals(note.name) or "")

		doc = frappe.get_doc("CRM Lead", lead)
		doc.lead_owner = REP
		doc.save(ignore_permissions=True)
		self.assertIn(
			f"|{REP}|", self._note_principals(note.name) or "",
			"the note kept its parent's OLD principals — a child row did not follow the reindex",
		)

	def _note_principals(self, note):
		rows = CRMLeadSearch().sql(
			"SELECT principals FROM search_fts WHERE doc_id = ?", [f"FCRM Note:{note}"], read_only=True
		)
		return rows[0]["principals"] if rows else None

	def test_a_share_is_a_principal(self):
		lead = self.other_in_a[3]
		frappe.share.add("CRM Lead", lead, REP, read=1, flags={"ignore_share_permission": True})
		self.addCleanup(frappe.db.delete, "DocShare", {"share_doctype": "CRM Lead", "share_name": lead})
		self.assertIn(f"|{REP}|", self._principals_column(lead) or "", "a shared lead did not carry its sharee")
		self.assertIn(lead, self._leads_found_by(REP)[0], "a shared lead was unfindable by the user it is shared with")

	def test_a_share_to_everyone_is_its_own_principal(self):
		"""`get_shared` treats `everyone=1` as reaching every logged-in user, and it names no user — so a
		per-user token cannot express it and the pre-filter would silently hide the lead."""
		lead = self.other_in_a[3]
		frappe.get_doc({
			"doctype": "DocShare", "share_doctype": "CRM Lead", "share_name": lead,
			"everyone": 1, "read": 1,
		}).insert(ignore_permissions=True)
		self.addCleanup(frappe.db.delete, "DocShare", {"share_doctype": "CRM Lead", "share_name": lead})
		self.assertIn("|everyone|", self._principals_column(lead) or "")
		self.assertIn("|everyone|", self._filters_as(REP)["principals"][1])
		self.assertIn(lead, self._leads_found_by(REP)[0], "a lead shared with everyone was unfindable")

	# --- 5. the RED proof, made permanent -----------------------------------------------------------
	#
	# The owner has uncommitted work across both repos, so "run the suite on the old code" is not a git
	# operation here. Instead the old design is reconstructed IN PLACE — its filter, its schema without the
	# permission column, and the framework's own unscoped result processing — and driven through the real
	# engine and a real index. These two tests are what makes "red before the fix" checkable at any time.

	def _as_the_old_code(self):
		schema = {
			**CRMLeadSearch.INDEX_SCHEMA,
			"metadata_fields": [f for f in CRMLeadSearch.INDEX_SCHEMA["metadata_fields"] if f != "principals"],
		}

		def old_filters(engine_self):
			from tatva_connect.access import entitlement

			if entitlement.entitled_grains() == entitlement.ALL_GRAINS:
				return {}
			return {"lead": frappe.get_list("CRM Lead", pluck="name", limit_page_length=0)}

		return (
			patch.object(CRMLeadSearch, "INDEX_SCHEMA", schema),
			patch.object(CRMLeadSearch, "get_search_filters", old_filters),
			patch.object(CRMLeadSearch, "_process_search_results", SQLiteSearch._process_search_results),
		)

	def test_the_old_enumeration_really_dies_at_a_lowered_variable_ceiling(self):
		"""The reported defect, reproduced: one bound variable per visible lead, a ceiling below that count,
		`sqlite3.OperationalError: too many SQL variables`, and a framework that logs it and returns []."""
		patches = self._as_the_old_code()
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		engine = CRMLeadSearch()
		engine.drop_index()
		engine.build_index()

		rep_found, _ = self._leads_found_by(REP, variable_limit=LOWERED_VARIABLE_LIMIT)
		mgr_found, _ = self._leads_found_by(MGR, variable_limit=LOWERED_VARIABLE_LIMIT)
		self.assertEqual(rep_found, set(), "the old enumeration was expected to blow the ceiling and it did not")
		self.assertEqual(mgr_found, set(), "the manager's silent-zero-results defect did not reproduce")
		# And with no ceiling lowered it worked — which is exactly why the defect went unnoticed.
		self.assertEqual(self._leads_found_by(REP)[0], set(self.rep_in_a))

	def test_dropping_the_authoritative_gate_really_leaks_a_lead_outside_the_grain(self):
		"""Proves the post-filter is load-bearing and not decoration: with the framework's own unscoped result
		processing restored, the pre-filter alone hands the rep a lead outside their entitled grain."""
		with patch.object(CRMLeadSearch, "_process_search_results", SQLiteSearch._process_search_results):
			leaked, _ = self._leads_found_by(REP)
		self.assertTrue(
			set(self.rep_in_b) & leaked,
			"the pre-filter was expected to be insufficient on its own — if it is not, this suite proves nothing",
		)
		self.assertEqual(set(self.rep_in_b) & self._leads_found_by(REP)[0], set())

	# --- 6. the rebuild this schema change depends on -----------------------------------------------

	def test_the_new_permission_column_moves_the_schema_fingerprint(self):
		"""P0's guard is what lands this column on a site that already has an index; a version constant is not
		needed because the fingerprint is derived from the declaration itself."""
		engine = CRMLeadSearch()
		self.assertIn("principals", engine.schema["metadata_fields"])
		self.assertEqual(engine.stored_fingerprint(), engine.schema_fingerprint())
		without = {
			**CRMLeadSearch.INDEX_SCHEMA,
			"metadata_fields": [f for f in CRMLeadSearch.INDEX_SCHEMA["metadata_fields"] if f != "principals"],
		}
		with patch.object(CRMLeadSearch, "INDEX_SCHEMA", without):
			self.assertNotEqual(engine.schema_fingerprint(), CRMLeadSearch().schema_fingerprint())
