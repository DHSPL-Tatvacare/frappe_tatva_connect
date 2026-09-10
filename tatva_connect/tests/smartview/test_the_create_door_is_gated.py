# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A REP MAY BUILD A VIEW, NOT PUBLISH ONE AT BIRTH — the create half of the write gate.

`access/ledger` grants create on `CRM Smart View` to Sales User, because a rep authoring their own view
is the point of the surface. That grant is narrowed by `smartview/permissions.py` and by nothing else, so
this locks the narrowing (A.20: the declaration IS the enforcement, and must go red when it drifts).

WHY THE HOOK AND NOT THE ENDPOINT. `upsert_view` saves with `ignore_permissions=True`, so it never asks
this question; the caller it protects against is the one that skips the endpoint entirely and POSTs to
`/api/resource/CRM Smart View`. `Document.insert` calls `check_permission("create")` BEFORE
`set_new_name()` (frappe document.py:457,461), so the doc has no name at that moment — which is exactly
why a gate written as "no name means nothing to judge" let this through.

WHAT A PUBLISHED, UNOWNED VIEW COSTS. `is_standard` offers the view to everyone entitled to its grain and
is `set_public`'s job, which rides `can_write`. `owner_user` IS `can_write`, so a view inserted without
one can never afterwards be edited or deleted by the person who made it — only a System Manager can
remove it. Set together at insert, a rep publishes something to their whole grain that nobody can retract.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import permissions as sv_perms

REP = "anaya.test@tatvacare.in"


def _doc(**kw):
	"""An unsaved view carrying whatever the caller is trying to set — what the hook is handed on insert."""
	return frappe.get_doc({"doctype": "CRM Smart View", "label": "lock probe", "base_object": "Lead", **kw})


class TestTheCreateDoorIsGated(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("User", REP):
			self.skipTest(f"{REP} is not on this site")

	def test_a_rep_may_create_a_view_of_their_own(self):
		"""The grant is real: nothing here may turn the surface off for the people it is built for."""
		self.assertTrue(sv_perms.may_create(_doc(), REP))
		self.assertTrue(sv_perms.may_create(_doc(owner_user=REP), REP))

	def test_a_rep_may_not_publish_at_birth(self):
		"""`is_standard` belongs to set_public, which asks can_write. Set at insert it skips that gate."""
		self.assertFalse(sv_perms.may_create(_doc(is_standard=1), REP))
		self.assertFalse(sv_perms.may_create(_doc(is_standard=1, owner_user=REP), REP))

	def test_a_rep_may_not_record_a_view_to_someone_else(self):
		"""`owner_user` IS the write gate — handing it away hands over a view nobody asked for."""
		self.assertFalse(sv_perms.may_create(_doc(owner_user="Administrator"), REP))

	def test_an_operator_is_untouched(self):
		"""A seeded standard view carries no owner_user; the operator path must keep working."""
		self.assertTrue(sv_perms.may_create(_doc(is_standard=1), "Administrator"))

	def test_the_hook_asks_on_the_ptype_not_the_absent_name(self):
		"""The regression itself: an inserting doc has no name yet, and reading that as "nothing to judge"
		is what left create open. Asked as `create`, the same doc is refused."""
		published = _doc(is_standard=1)
		self.assertFalse(sv_perms.has_smart_view_permission(published, "create", REP))
		# And the shape that hid it: the very same doc, asked the way the old gate asked.
		self.assertFalse(published.get("name"), "the probe must be an unsaved doc for this lock to mean anything")

	def test_the_native_door_actually_refuses(self):
		"""End to end through frappe's own insert, which is the door the endpoint cannot see."""
		frappe.set_user(REP)
		try:
			with self.assertRaises(frappe.PermissionError):
				_doc(is_standard=1).insert()
		finally:
			frappe.set_user("Administrator")
			frappe.db.rollback()
