# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE AUTHORIZATION — every view the surface OFFERS must OPEN, for the same caller, by the same rule.

THE DEFECT THIS LOCKS (SV-01). Three matchers answered "may I use this view" and two pointed in opposite
directions: the tab row asked `_contract_covers(view, caller)` (view = contract, blank = wildcard), the
row path asked `grain_entitled(view)` → `covers(caller, view)` (view = data, blank = the literal empty
string). A vertical-wide standard view, and every cross-grain shared view, was offered and then refused
with "You are not entitled to this grain." A saved view's grain is a RULE grain; the one right question
is `entitlement.grain_overlaps_entitlement`, and after this pass exactly one function asks it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_one_authorization
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.smartview import api as smartview

PREFIX = "ZZ OneAuth View"
V1, V2 = "ZZ OA Vertical One", "ZZ OA Vertical Two"
G1, G2 = "ZZ OA Group One", "ZZ OA Group Two"
REP = "zz-oneauth-rep@example.com"
STRANGER = "zz-oneauth-stranger@example.com"


class _OneAuthCase(FrappeTestCase):
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
		for email, first in ((REP, "OneAuth Rep"), (STRANGER, "OneAuth Stranger")):
			if not frappe.db.exists("User", email):
				frappe.get_doc({"doctype": "User", "email": email, "first_name": first,
				                "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}
				               ).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for dt, name in (("User", REP), ("User", STRANGER), ("CRM Vertical", V1), ("CRM Vertical", V2),
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

	def _as(self, user, grains):
		frappe.set_user(user)
		return patch.object(entitlement, "entitled_grains", return_value=grains)


class TestEveryOfferedViewOpens(_OneAuthCase):
	"""The tab row and the row path answer with ONE rule — offered means openable."""

	def test_every_offered_view_returns_rows(self):
		# RED on api.py:288 `_grains_for_view` -> `_grains_from_axes` -> api.py:325 grain_entitled throw.
		self._view("wide", vertical=V1, group="", standard=1)
		self._view("exact", vertical=V1, group=G1, standard=1)
		with self._as(REP, {(V1, G1, "")}):
			try:
				offered = [t["name"] for t in smartview.get_smart_views()]
				self.assertTrue(offered, "nothing offered — fixture broke, not the rule under test")
				for name in offered:
					smartview.get_data(name, page_size=1)
			finally:
				frappe.set_user("Administrator")

	def test_a_cross_grain_shared_view_returns_rows(self):
		# RED on the same line: the share admits the tab, then get_data asks grain_entitled and throws.
		other = self._view("other line", vertical=V2, group=G2, standard=0, owner=STRANGER)
		frappe.share.add_docshare("CRM Smart View", other, REP, read=1,
		                          flags={"ignore_share_permission": True})
		frappe.db.commit()
		with self._as(REP, {(V1, G1, "")}):
			try:
				self.assertIn(other, [t["name"] for t in smartview.get_smart_views()])
				smartview.get_data(other, page_size=1)
			finally:
				frappe.set_user("Administrator")


class TestGetDataHasAGate(_OneAuthCase):
	"""SV-02: the data path checks the same rule the open path does — today it checks nothing."""

	def test_a_stranger_cannot_read_a_private_views_data(self):
		# RED on api.py:781: get_data loads the doc and never asks may-this-caller-read (columns leak).
		private = self._view("private", vertical="", group="", standard=0, owner=REP)
		with self._as(STRANGER, {(V1, G1, "")}):
			try:
				with self.assertRaises(frappe.PermissionError):
					smartview.get_data(private, page_size=1)
			finally:
				frappe.set_user("Administrator")


class TestARepCanShareTheirOwnView(_OneAuthCase):
	"""SV-07: sharing runs as the OWNER, a plain Sales User — never as Administrator."""

	def test_owner_shares_without_a_docperm(self):
		# RED on frappe/share.py:56: check_share_permission needs a share DocPerm no Sales User holds.
		mine = self._view("mine to share", vertical="", group="", standard=0, owner=REP)
		frappe.set_user(REP)
		try:
			smartview.share_view(mine, STRANGER)
		finally:
			frappe.set_user("Administrator")
		self.assertTrue(frappe.db.exists("DocShare", {
			"share_doctype": "CRM Smart View", "share_name": mine, "user": STRANGER,
		}))

	def test_a_reader_still_cannot_share_onward(self):
		# GREEN today by accident (frappe refused everyone); stays green because OUR gate refuses readers.
		mine = self._view("mine kept", vertical="", group="", standard=0, owner=REP)
		frappe.share.add_docshare("CRM Smart View", mine, STRANGER, read=1,
		                          flags={"ignore_share_permission": True})
		frappe.db.commit()
		frappe.set_user(STRANGER)
		try:
			with self.assertRaises(frappe.PermissionError):
				smartview.share_view(mine, REP)
		finally:
			frappe.set_user("Administrator")


class TestGrainMatchIsCaseInsensitive(_OneAuthCase):
	"""SV-11: MariaDB calls two casings ONE key; every matcher must agree with the database."""

	def test_a_view_stored_in_another_case_is_still_offered_and_opens(self):
		"""RED on the retired `entitlement._contract_covers`, which compared axes with `==` while the one
		home (`taxonomy.grain.same`) casefolds because MariaDB's ci collation calls the two spellings ONE
		key. Written with db.set_value, not insert: frappe's Link validation may canonicalise the value on
		save, which would test the framework instead of the matcher."""
		swapped = V1.swapcase()
		view = self._view("cased", vertical=V1, group="", standard=1)
		frappe.db.set_value("CRM Smart View", view, "vertical", swapped, update_modified=False)
		self.assertEqual(frappe.db.get_value("CRM Smart View", view, "vertical"), swapped)
		with self._as(REP, {(V1, G1, "")}):
			try:
				self.assertIn(view, [t["name"] for t in smartview.get_smart_views()])
				smartview.get_data(view, page_size=1)
			finally:
				frappe.set_user("Administrator")
