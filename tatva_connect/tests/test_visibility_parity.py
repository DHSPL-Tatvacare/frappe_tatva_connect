# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Parity gate for the row-visibility refactor (Leg 2, Batch 0).

The standalone CRM Task permission logic that used to live in tasks/permissions.py folded
into the shared brain access/visibility.py. This proves the brain reproduces that logic
EXACTLY for CRM Task: same pqc SQL string and same has_permission allow/deny for a Sales
User and a Sales Manager, on a task they own, a task on a visible parent, and a task on a
hidden parent. The pre-refactor logic is frozen here as a reference re-implementation
(`_legacy_pqc` / `_legacy_has_perm`) so the assertion is against the real prior behaviour,
not a hand-typed SQL literal.
"""
import frappe
from frappe.model.db_query import DatabaseQuery
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import visibility
from tatva_connect.automation import seed

_SWITCH = "Task::CRM Task::visibility"
_PARENTS = ("CRM Lead", "CRM Deal")


# --- frozen pre-refactor logic (verbatim from the old tasks/permissions.py) --------------
def _legacy_subquery(parent, user):
	try:
		cond = DatabaseQuery(parent, user=user).build_match_conditions(as_condition=True)
	except frappe.PermissionError:
		cond = "1=0"
	where = f" and ({cond})" if cond else ""
	return f"(select `name` from `tab{parent}` where 1=1{where})"


def _legacy_pqc(user):
	if not frappe.db.get_value("CRM Tatva Automation", _SWITCH, "enabled"):
		return ""
	if user == "Administrator" or "System Manager" in frappe.get_roles(user):
		return ""
	tbl = "`tabCRM Task`"
	u = frappe.db.escape(user)
	clauses = [f"{tbl}.`owner`={u}", f"{tbl}.`assigned_to`={u}"]
	for parent in _PARENTS:
		clauses.append(
			f"({tbl}.`reference_doctype`={frappe.db.escape(parent)} "
			f"and {tbl}.`reference_docname` in {_legacy_subquery(parent, user)})"
		)
	return "(" + " or ".join(clauses) + ")"


def _legacy_has_perm(doc, user):
	if not frappe.db.get_value("CRM Tatva Automation", _SWITCH, "enabled"):
		return True
	if user == "Administrator" or "System Manager" in frappe.get_roles(user):
		return True
	if doc.owner == user or doc.get("assigned_to") == user:
		return True
	if (
		doc.reference_docname
		and doc.reference_doctype in _PARENTS
		and frappe.db.exists(doc.reference_doctype, doc.reference_docname)
	):
		return frappe.has_permission(doc.reference_doctype, "read", doc.reference_docname, user=user)
	return False


class TestVisibilityParity(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
		frappe.db.set_value("CRM Tatva Automation", _SWITCH, "enabled", 1)
		self._made = []
		self.manager = self._user("vis-mgr@example.com", "Sales Manager")
		self.rep = self._user("vis-rep@example.com", "Sales User")
		# A lead the manager owns; a task on it; an orphan task; an own task for the rep.
		self.lead = self._lead(self.manager)
		self.task_on_lead = self._task(reference=("CRM Lead", self.lead), owner=self.manager)
		self.task_orphan = self._task(reference=None, owner=self.manager)
		self.task_rep_own = self._task(reference=("CRM Lead", self.lead), owner=self.rep)

	def tearDown(self):
		frappe.set_user("Administrator")
		for dt, name in reversed(self._made):
			frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.set_value("CRM Tatva Automation", _SWITCH, "enabled", 0)

	# --- helpers ---------------------------------------------------------
	def _user(self, email, role):
		if not frappe.db.exists("User", email):
			u = frappe.get_doc({
				"doctype": "User", "email": email, "first_name": email.split("@")[0],
				"send_welcome_email": 0, "roles": [{"role": role}],
			})
			u.insert(ignore_permissions=True)
		else:
			u = frappe.get_doc("User", email)
			if role not in [r.role for r in u.roles]:
				u.add_roles(role)
		return email

	def _lead(self, owner):
		ld = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Parity", "lead_name": "Parity Lead",
			"status": "New", "lead_owner": owner,
		})
		ld.insert(ignore_permissions=True)
		ld.db_set("owner", owner)
		self._made.append(("CRM Lead", ld.name))
		return ld.name

	def _task(self, reference, owner):
		data = {"doctype": "CRM Task", "title": "Parity Task", "status": "Todo"}
		if reference:
			data["reference_doctype"], data["reference_docname"] = reference
		t = frappe.get_doc(data)
		t.insert(ignore_permissions=True)
		t.db_set("owner", owner)
		self._made.append(("CRM Task", t.name))
		return frappe.get_doc("CRM Task", t.name)

	# --- pqc parity ------------------------------------------------------
	def test_pqc_string_parity(self):
		for user in (self.rep, self.manager):
			self.assertEqual(
				visibility.scoped_pqc("CRM Task", user),
				_legacy_pqc(user),
				f"pqc drift for {user}",
			)

	def test_pqc_off_is_stock(self):
		frappe.db.set_value("CRM Tatva Automation", _SWITCH, "enabled", 0)
		self.assertEqual(visibility.scoped_pqc("CRM Task", self.rep), "")

	# --- has_permission parity ------------------------------------------
	def test_has_permission_parity(self):
		cases = [self.task_on_lead, self.task_orphan, self.task_rep_own]
		for user in (self.rep, self.manager):
			for doc in cases:
				self.assertEqual(
					visibility.scoped_has_permission(doc, "read", user),
					_legacy_has_perm(doc, user),
					f"has_permission drift: {user} on {doc.name}",
				)

	def test_owner_always_visible(self):
		# rep owns task_rep_own -> visible regardless of parent scope.
		self.assertTrue(visibility.scoped_has_permission(self.task_rep_own, "read", self.rep))

	def test_orphan_denied_for_non_owner(self):
		# rep does not own the orphan task and there's no parent -> fail-closed deny.
		self.assertFalse(visibility.scoped_has_permission(self.task_orphan, "read", self.rep))
