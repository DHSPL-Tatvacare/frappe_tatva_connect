# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Spotlight visibility is frappe's get_list alone: the index stores no owner, assignee or share.
So a caller sees only their own line, and a reassignment makes a lead visible without any reindex."""
import os
import shutil
from unittest.mock import patch

import frappe
from frappe.search.sqlite_search import SQLiteSearch
from frappe.tests import IntegrationTestCase

from tatva_connect.search.index import _PENDING, TOGGLE, CRMLeadSearch

VERTICAL_A = "ZZ Search Scope A"
VERTICAL_B = "ZZ Search Scope B"
GROUP = "ZZ Search Scope Group"
REP = "zz-search-rep@example.test"
MANAGER = "zz-search-manager@example.test"
OTHER = "zz-search-other@example.test"
# One rare word in every fixture lead's name, so one query reaches exactly these leads.
TOKEN = "zzsearchscopepatient"
PHONE_PREFIX = "+91610009"


class TestSearchPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_purge()
		for vertical in (VERTICAL_A, VERTICAL_B):
			if not frappe.db.exists("CRM Vertical", vertical):
				frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": vertical}).insert(ignore_permissions=True)
		if not frappe.db.exists("CRM Group", GROUP):
			frappe.get_doc({"doctype": "CRM Group", "group_name": GROUP}).insert(ignore_permissions=True)
		# The rep is narrowed to owned + assigned leads; the manager is outside the hierarchy and narrowed by nothing.
		_user(REP, "Sales User", VERTICAL_A)
		_user(MANAGER, "Sales Manager", VERTICAL_A)
		_user(OTHER, "Sales User", VERTICAL_B)
		cls.rep_in_a = [_lead(VERTICAL_A, REP, n) for n in range(3)]
		cls.other_in_a = [_lead(VERTICAL_A, OTHER, n) for n in range(3, 5)]
		cls.rep_in_b = [_lead(VERTICAL_B, REP, n) for n in range(5, 7)]
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_purge()
		for email in (REP, MANAGER, OTHER):
			frappe.db.delete("User Permission", {"user": email})
			frappe.db.delete("Has Role", {"parent": email, "parenttype": "User"})
			frappe.db.delete("User", {"name": email})
		for doctype, name in (("CRM Group", GROUP), ("CRM Vertical", VERTICAL_A), ("CRM Vertical", VERTICAL_B)):
			frappe.db.delete(doctype, {"name": name})
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user("Administrator")
		# Record every enqueue instead of running it, so a test can assert no reindex was scheduled.
		self.enqueued = []
		recorder = patch("frappe.enqueue", lambda method, **_kwargs: self.enqueued.append(method))
		recorder.start()
		self.addCleanup(recorder.stop)
		frappe.db.set_value("CRM Tatva Automation", TOGGLE, "enabled", 1)
		# A lead save can commit, so each test starts from the declared owners with no assignment or share.
		for leads, owner in ((self.rep_in_a, REP), (self.other_in_a, OTHER), (self.rep_in_b, REP)):
			for lead in leads:
				_detach(lead)
				frappe.db.set_value("CRM Lead", lead, "lead_owner", owner, update_modified=False)
		# The index is a file the rollback cannot undo, so the site's own is set aside and restored.
		engine = CRMLeadSearch()
		self.backup = f"{engine.db_path}.search-permissions.bak"
		if os.path.exists(engine.db_path):
			shutil.copy2(engine.db_path, self.backup)
		self.addCleanup(_restore, engine.db_path, self.backup)
		engine.drop_index()
		engine.build_index()

	def _found_by(self, user):
		"""The leads this user's own spotlight search returns, through the real engine and index."""
		frappe.set_user(user)
		try:
			return {row.get("lead") for row in (CRMLeadSearch().search(TOKEN) or {}).get("results", [])}
		finally:
			frappe.set_user("Administrator")

	def _reindex_scheduled(self):
		# Leads collected for this transaction's reindex, plus any search job already enqueued.
		pending = getattr(frappe.local, _PENDING, None) or set()
		return [*pending, *(m for m in self.enqueued if str(m).startswith("tatva_connect.search."))]

	def test_each_caller_sees_only_their_own_line(self):
		self.assertEqual(self._found_by(REP), set(self.rep_in_a))
		self.assertEqual(self._found_by(MANAGER), {*self.rep_in_a, *self.other_in_a})
		self.assertEqual(self._found_by("Administrator"), {*self.rep_in_a, *self.other_in_a, *self.rep_in_b})

	def test_a_lead_outside_the_grain_is_never_returned_even_to_its_owner(self):
		self.assertFalse((self._found_by(REP) | self._found_by(MANAGER)) & set(self.rep_in_b))

	def test_an_assignment_makes_the_lead_visible_without_a_reindex(self):
		lead = self.other_in_a[0]
		self.assertNotIn(lead, self._found_by(REP))
		frappe.get_doc({
			"doctype": "ToDo", "reference_type": "CRM Lead", "reference_name": lead,
			"allocated_to": REP, "status": "Open", "description": "search scope assignment",
		}).insert(ignore_permissions=True)
		self.assertIn(lead, self._found_by(REP))
		self.assertFalse(self._reindex_scheduled(), "an assignment scheduled a search reindex")

	def test_a_reassigned_owner_sees_the_lead_without_a_reindex(self):
		doc = frappe.get_doc("CRM Lead", self.other_in_a[1])
		doc.lead_owner = REP
		doc.save(ignore_permissions=True)
		self.assertIn(doc.name, self._found_by(REP))
		self.assertFalse(self._reindex_scheduled(), "a reassignment scheduled a search reindex")

	def test_removing_the_get_list_gate_really_leaks(self):
		"""Mutation proof: with frappe's unscoped result processing, the rep sees other lines' leads."""
		with patch.object(CRMLeadSearch, "_process_search_results", SQLiteSearch._process_search_results):
			self.assertTrue(self._found_by(REP) - set(self.rep_in_a), "the gate was expected to be load-bearing")


def _restore(db_path, backup):
	if os.path.exists(backup):
		shutil.move(backup, db_path)
	elif os.path.exists(db_path):
		os.unlink(db_path)


def _detach(lead):
	frappe.db.delete("ToDo", {"reference_type": "CRM Lead", "reference_name": lead})
	frappe.db.delete("DocShare", {"share_doctype": "CRM Lead", "share_name": lead})


def _purge():
	for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", f"{PHONE_PREFIX}%"]}, pluck="name"):
		_detach(name)
		frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)


def _user(email, role, vertical):
	if not frappe.db.exists("User", email):
		user = frappe.get_doc({
			"doctype": "User", "email": email, "first_name": "ZZ Search",
			"send_welcome_email": 0, "user_type": "System User",
		}).insert(ignore_permissions=True)
		user.add_roles(role)
	if not frappe.db.exists("User Permission", {"user": email, "allow": "CRM Vertical"}):
		frappe.get_doc({
			"doctype": "User Permission", "user": email,
			"allow": "CRM Vertical", "for_value": vertical, "applicable_for": "CRM Lead",
		}).insert(ignore_permissions=True)


def _lead(vertical, owner, n):
	# Owner set after insert: crm's controller would otherwise share the lead, and a share bypasses the grain.
	name = frappe.get_doc({
		"doctype": "CRM Lead", "first_name": TOKEN, "last_name": f"N{n}", "status": "New",
		"mobile_no": f"{PHONE_PREFIX}{n:04d}", "custom_vertical": vertical, "custom_group": GROUP,
	}).insert(ignore_permissions=True).name
	frappe.db.set_value("CRM Lead", name, "lead_owner", owner, update_modified=False)
	return name
