# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A STANDARD VIEW IS OFFERED TO ITS OWN GRAIN, NOT TO THE WHOLE SITE.

`is_standard` has always read "shown to every user IN THE GRAIN", and the doctype has always stored the
three axes — but the listing ignored them and handed every standard view to every user on the site. With
eighty task types across several business lines that is not clutter: it is one line's curated worklists
sitting in another line's sidebar, labelled with that line's language.

THE MATCH IS THE APP'S ONE RULE, not a new one. `entitlement._contract_covers`: a SET axis on the view
must equal the caller's, a BLANK axis is a wildcard. So a view declared for a whole vertical reaches
every group inside it, exactly as a field contract does. Comparing the tuples directly instead is the
defect that once hid 129 fields from 1,894 leads, and it would hide a vertical-wide view from everybody.

WHAT THIS DOES NOT DECIDE. It decides what is OFFERED. The rows inside a view are always the viewer's
own — the composer ANDs their permission conditions on every run — so a mis-scoped view could never have
leaked a record. It leaked the SHAPE of another line's work, which is its own problem.

Personal and shared views are deliberately exempt: the first is the caller's, the second was handed over
on purpose by someone who could.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_views_are_offered_by_grain
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.smartview import api as smartview

PREFIX = "ZZ Grain View"
V1, V2 = "ZZ GV Vertical One", "ZZ GV Vertical Two"
G1, G2 = "ZZ GV Group One", "ZZ GV Group Two"
USER = "zz-grain-view@example.com"


class TestViewsAreOfferedByGrain(FrappeTestCase):
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
		if not frappe.db.exists("User", USER):
			frappe.get_doc({"doctype": "User", "email": USER, "first_name": "Grain Viewer",
			                "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}
			               ).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for dt, name in (("User", USER), ("CRM Group", G1), ("CRM Group", G2),
		                 ("CRM Vertical", V1), ("CRM Vertical", V2)):
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
		self.mine_v1 = self._view("mine v1", vertical=V1, group=G1, standard=1)
		self.other_v2 = self._view("other v2", vertical=V2, group=G2, standard=1)
		self.wide_v1 = self._view("wide v1", vertical=V1, group="", standard=1)
		self.no_grain = self._view("no grain", vertical="", group="", standard=1)
		self.personal = self._view("personal", vertical=V2, group=G2, standard=0, owner=USER)
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

	def _offered(self, grains):
		"""The tabs this caller is offered, with their entitlement stated."""
		frappe.set_user(USER)
		try:
			with patch.object(entitlement, "entitled_grains", return_value=grains):
				return {t["name"] for t in smartview.get_smart_views()}
		finally:
			frappe.set_user("Administrator")

	# ---- the scoping ---------------------------------------------------------------------------------

	def test_a_view_of_my_grain_is_offered(self):
		self.assertIn(self.mine_v1, self._offered({(V1, G1, "")}))

	def test_another_business_lines_view_is_not(self):
		"""THE POINT. Before this, every standard view on the site landed in everyone's sidebar."""
		self.assertNotIn(self.other_v2, self._offered({(V1, G1, "")}))

	def test_a_vertical_wide_view_reaches_every_group_inside_it(self):
		"""A blank axis is a WILDCARD, never the empty string — the app's one grain rule. Compared as a
		plain tuple this view would be offered to nobody at all."""
		self.assertIn(self.wide_v1, self._offered({(V1, G1, "")}))
		self.assertIn(self.wide_v1, self._offered({(V1, G2, "")}))
		self.assertNotIn(self.wide_v1, self._offered({(V2, G2, "")}))

	def test_a_view_declared_for_no_grain_is_site_wide(self):
		"""Deliberate: a view naming no axis at all is not scoped to nothing, it is scoped to everyone."""
		self.assertIn(self.no_grain, self._offered({(V1, G1, "")}))
		self.assertIn(self.no_grain, self._offered({(V2, G2, "")}))

	def test_a_caller_with_no_entitlement_is_offered_no_standard_view(self):
		offered = self._offered(set())
		self.assertNotIn(self.mine_v1, offered)
		self.assertNotIn(self.other_v2, offered)

	def test_a_system_manager_still_sees_everything(self):
		offered = self._offered(entitlement.ALL_GRAINS)
		for v in (self.mine_v1, self.other_v2, self.wide_v1):
			self.assertIn(v, offered)

	# ---- what grain must NOT touch -------------------------------------------------------------------

	def test_my_own_view_is_mine_whatever_its_grain(self):
		"""A personal view on another grain is still the caller's own — scoping it away would delete their
		work from their own sidebar."""
		self.assertIn(self.personal, self._offered({(V1, G1, "")}))

	def test_a_view_shared_with_me_is_offered_across_grains(self):
		"""A share is deliberate: someone who could share it decided this person should have it. Scoping
		it away would make sharing across business lines silently do nothing."""
		frappe.set_user("Administrator")
		smartview.share_view(self.other_v2, USER)
		frappe.db.commit()
		self.assertIn(self.other_v2, self._offered({(V1, G1, "")}))

	def test_scoping_decides_what_is_offered_not_what_is_readable(self):
		"""The rows inside every view are the VIEWER's own, whatever the tab list says — so a mis-scoped
		view was never a data leak, and this change is not what makes the rows safe."""
		import inspect

		source = inspect.getsource(smartview.get_data)
		self.assertIn("visibility.readable_criterion", source,
		              "the composer no longer applies the viewer's own permission conditions")
