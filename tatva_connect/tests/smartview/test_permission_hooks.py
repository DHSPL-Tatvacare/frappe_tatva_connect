# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE TWO HOOKS — the restrictive backstop must narrow a native read, and must never deny a grant.

`smartview/permissions.py` registers two hooks (hooks.py:114, :125) and the module docstring makes two
load-bearing claims about them. Neither had a test, and both are the kind of claim that is right until
someone edits the predicate:

  CLAIM 1 — the `has_permission` hook is DENY-ONLY, so it must never be the thing that refuses an
  operator or a DocShare recipient. Verified in frappe source: `get_doc_permissions`
  (permissions.py:235) calls `has_controller_permissions` FIRST and a False short-circuits to
  `{ptype: 0}` before role permissions are even read. `has_permission` has a share rescue further down
  (permissions.py:207), but `get_doc_permissions` — what the Desk form loader reads — does not. So a
  wrong deny here silently removes a shared view from Desk.

  CLAIM 2 — the `permission_query_conditions` hook makes a NATIVE list read no looser than the app door.
  It is driven by nothing today: the doctype's DocPerms are System-Manager-only, and an operator returns
  "" from the hook, so on this bench the SQL never runs at all. The scenario it exists for is an operator
  granting a role read access — that is what this test stages, with a Custom DocPerm it removes again.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_permission_hooks
"""
from unittest.mock import patch

import frappe
from frappe.permissions import add_permission, has_controller_permissions, reset_perms
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.smartview import permissions as sv_perms

PREFIX = "ZZ Hook View"
V1, V2 = "ZZ Hook Vertical One", "ZZ Hook Vertical Two"
G1, G2 = "ZZ Hook Group One", "ZZ Hook Group Two"
REP = "zz-hook-rep@example.com"
OWNER = "zz-hook-owner@example.com"
REP_ROLE = "Sales User"


class _HookCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for name in (V1, V2):
			if not frappe.db.exists("CRM Vertical", name):
				frappe.get_doc({"doctype": "CRM Vertical", "vertical_name": name}).insert(ignore_permissions=True)
		for name in (G1, G2):
			if not frappe.db.exists("CRM Group", name):
				frappe.get_doc({"doctype": "CRM Group", "group_name": name}).insert(ignore_permissions=True)
		for email, first in ((REP, "Hook Rep"), (OWNER, "Hook Owner")):
			if not frappe.db.exists("User", email):
				frappe.get_doc({"doctype": "User", "email": email, "first_name": first,
				                "send_welcome_email": 0, "roles": [{"role": REP_ROLE}]}
				               ).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for dt, name in (("User", REP), ("User", OWNER), ("CRM Vertical", V1), ("CRM Vertical", V2),
		                 ("CRM Group", G1), ("CRM Group", G2)):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@staticmethod
	def _purge():
		for name in frappe.get_all("CRM Smart View", filters={"label": ["like", f"{PREFIX}%"]}, pluck="name"):
			frappe.db.delete("DocShare", {"share_doctype": "CRM Smart View", "share_name": name})
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge()
		frappe.db.commit()

	def _view(self, tag, vertical="", group="", standard=1, owner=None):
		return frappe.get_doc({
			"doctype": "CRM Smart View", "label": f"{PREFIX} {tag}", "base_object": "Lead",
			"is_standard": standard, "owner_user": owner,
			"vertical": vertical or None, "group": group or None,
			"columns": frappe.as_json([]),
		}).insert(ignore_permissions=True).name

	def _entitled(self, grains):
		return patch.object(entitlement, "entitled_grains", return_value=grains)


class TestTheDenyOnlyHookNeverDenies(_HookCase):
	"""CLAIM 1 — driven through frappe's OWN dispatcher, so it proves the hooks.py wiring too."""

	def _controller_says(self, name, user, ptype="read"):
		"""frappe's own hook dispatcher, reading `has_permission` out of hooks.py. Deliberately not a
		direct call to our function: that would test the predicate and leave the wiring unproven."""
		return has_controller_permissions(frappe.get_doc("CRM Smart View", name), ptype, user=user)

	def test_a_docshare_recipient_is_not_denied_by_the_controller(self):
		"""RED if the hook ever answers on role/grain alone: a share is the one route with no grain."""
		other = self._view("shared across lines", vertical=V2, group=G2, standard=0, owner=OWNER)
		frappe.share.add_docshare("CRM Smart View", other, REP, read=1,
		                          flags={"ignore_share_permission": True})
		frappe.db.commit()
		with self._entitled({(V1, G1, "")}):
			self.assertTrue(self._controller_says(other, REP),
			                "the deny-only hook refused a DocShare recipient — Desk loses the shared view")
			# End to end, with the share rescue in play, so the whole chain is proven and not just the hook.
			self.assertTrue(frappe.has_permission("CRM Smart View", "read", doc=other, user=REP))

	def test_an_operator_is_never_denied(self):
		"""An operator reaches every view including another rep's private one."""
		private = self._view("someone elses private", standard=0, owner=OWNER)
		admin_sm = frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User"},
		                          pluck="parent", limit=1)
		self.assertTrue(admin_sm, "no System Manager on this site — fixture problem, not the rule")
		with self._entitled(set()):
			self.assertTrue(self._controller_says(private, admin_sm[0]))

	def test_the_owner_of_a_private_view_is_not_denied(self):
		"""The owner route carries no grain either — a view a rep saved for themselves stays theirs."""
		mine = self._view("my own", standard=0, owner=REP)
		with self._entitled(set()):
			self.assertTrue(self._controller_says(mine, REP))

	def test_the_hook_does_refuse_a_stranger(self):
		"""The backstop is a backstop: it still denies where the predicate positively refuses, or it is
		decoration. RED if `has_smart_view_permission` is ever loosened to a blanket True."""
		private = self._view("not mine", standard=0, owner=OWNER)
		with self._entitled({(V1, G1, "")}):
			self.assertFalse(self._controller_says(private, REP))

	def test_write_shaped_ptypes_ask_the_write_predicate(self):
		"""A reader of a shared view may open it and may not edit it — one predicate each, not one for both."""
		other = self._view("shared read only", standard=0, owner=OWNER)
		frappe.share.add_docshare("CRM Smart View", other, REP, read=1,
		                          flags={"ignore_share_permission": True})
		frappe.db.commit()
		with self._entitled({(V1, G1, "")}):
			self.assertTrue(self._controller_says(other, REP, ptype="read"))
			self.assertFalse(self._controller_says(other, REP, ptype="write"))


class TestTheQueryConditionsNarrowANativeRead(_HookCase):
	"""CLAIM 2 — a real `frappe.get_list`, not a string comparison on the generated SQL.

	The DocPerms are System-Manager-only, so a rep's native read is refused before the hook is consulted
	and the SQL is exercised by nothing. This stages the scenario the hook exists for — an operator grants
	a role read access — with a Custom DocPerm that is removed again in tearDown. `reset_perms` deletes
	every Custom DocPerm for the doctype, so the test first proves there were none to lose."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._had_custom_perms = bool(frappe.get_all("Custom DocPerm", filters={"parent": "CRM Smart View"}))

	def setUp(self):
		super().setUp()
		if self._had_custom_perms:
			self.skipTest("CRM Smart View already carries operator-set Custom DocPerms — refusing to reset them")
		add_permission("CRM Smart View", REP_ROLE, 0)
		frappe.clear_cache(doctype="CRM Smart View")

	def tearDown(self):
		frappe.set_user("Administrator")
		reset_perms("CRM Smart View")
		frappe.clear_cache(doctype="CRM Smart View")
		super().tearDown()

	def _list_as(self, user):
		frappe.set_user(user)
		try:
			return set(frappe.get_list("CRM Smart View", pluck="name", limit=0))
		finally:
			frappe.set_user("Administrator")

	def test_a_native_get_list_returns_exactly_the_readable_set(self):
		"""RED with the hook unregistered: the role perm alone returns every row in the table."""
		mine = self._view("native mine", standard=0, owner=REP)
		entitled = self._view("native entitled", vertical=V1, group=G1, standard=1)
		shared = self._view("native shared", vertical=V2, group=G2, standard=0, owner=OWNER)
		frappe.share.add_docshare("CRM Smart View", shared, REP, read=1,
		                          flags={"ignore_share_permission": True})
		off_line = self._view("native off line", vertical=V2, group=G2, standard=1)
		private = self._view("native someone elses", standard=0, owner=OWNER)
		frappe.db.commit()

		with self._entitled({(V1, G1, "")}):
			got = self._list_as(REP)
			self.assertLessEqual({mine, entitled, shared}, got)
			self.assertNotIn(off_line, got, "an unentitled standard view leaked into a native list read")
			self.assertNotIn(private, got, "another rep's private view leaked into a native list read")
			# The one predicate decides both surfaces, so the two answers are the SAME set, not merely close.
			expected = {r.name for r in sv_perms.readable_views(REP)}
			self.assertEqual(got & self._ours(), expected & self._ours())

	def test_nothing_readable_selects_nothing_not_everything(self):
		"""The `1=0` branch. A deny that returns "" would hand a rep the whole table."""
		self._view("native off line only", vertical=V2, group=G2, standard=1)
		frappe.db.commit()
		with self._entitled(set()):
			self.assertEqual(self._list_as(REP) & self._ours(), set())

	def test_an_operator_reads_the_whole_table(self):
		"""The hook returns "" for an operator, so the narrowing must not apply to them."""
		off_line = self._view("native operator sees this", vertical=V2, group=G2, standard=1)
		frappe.db.commit()
		self.assertIn(off_line, self._list_as("Administrator"))

	def _ours(self):
		"""Only this test's fixtures, so a real seeded view on the bench cannot make the assertion lie."""
		return set(frappe.get_all("CRM Smart View", filters={"label": ["like", f"{PREFIX}%"]}, pluck="name"))
