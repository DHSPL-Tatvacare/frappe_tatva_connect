# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A DRAGGED COLUMN WIDTH IS REMEMBERED — and it is the only thing that write may do.

The grid already resized live; nothing persisted it, so every reload snapped the columns back to the
width their fieldtype implies. The Leads list solves this by writing the width onto its saved view
(`Leads.vue` → `ViewControls.updateColumns`); this is the same idea on `CRM Smart View`, stored BESIDE
the column set rather than inside it, so `columns` stays a plain validated list of field keys.

WHAT THIS SUITE IS REALLY GUARDING. The stored value is echoed back into a style attribute by the grid,
and dragging a column is not authoring a view. So the endpoint has to be narrow in three ways, and each
one is asserted below:

  * only a plain CSS length is stored — anything else is dropped, never escaped-and-kept;
  * only a key the view actually projects is stored, so it cannot become a second column set;
  * only a caller who may WRITE the view may store one, and one who may not is refused quietly rather
    than shown an error for dragging a column.

It must also not look like an edit: `modified` is untouched, or every drag would land in the audit trail
as a change to the view's definition.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_column_widths_persist
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as smartview

LABEL = "ZZ Width View"


class TestColumnWidthsPersist(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self._purge()
		cat = smartview.field_catalog("Lead")
		self.keys = [c["field_key"] for c in cat[:3]]
		self.view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": LABEL, "base_object": "Lead", "is_standard": 1,
			"columns": frappe.as_json(self.keys),
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._purge()
		frappe.db.commit()

	@staticmethod
	def _purge():
		for name in frappe.get_all("CRM Smart View", filters={"label": LABEL}, pluck="name"):
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)

	def _stored(self):
		return frappe.parse_json(
			frappe.db.get_value("CRM Smart View", self.view, "column_widths") or "{}"
		)

	# ---- it remembers -------------------------------------------------------------------------------

	def test_a_width_is_remembered(self):
		smartview.set_column_widths(self.view, {self.keys[0]: "18rem"})
		self.assertEqual(self._stored(), {self.keys[0]: "18rem"})

	def test_the_list_reads_it_back_on_the_next_paint(self):
		"""The grid applies this on its FIRST paint, so it must ride the tab payload the store already
		holds — fetched separately, the columns would render at default widths and then jump."""
		smartview.set_column_widths(self.view, {self.keys[0]: "18rem"})
		tab = next(t for t in smartview.get_smart_views() if t["name"] == self.view)
		self.assertEqual(tab["column_widths"], {self.keys[0]: "18rem"})
		self.assertEqual(smartview.get_view(self.view)["column_widths"], {self.keys[0]: "18rem"})

	def test_a_second_drag_replaces_the_first(self):
		smartview.set_column_widths(self.view, {self.keys[0]: "18rem"})
		smartview.set_column_widths(self.view, {self.keys[0]: "24rem"})
		self.assertEqual(self._stored(), {self.keys[0]: "24rem"})

	# ---- and it may do nothing else -----------------------------------------------------------------

	def test_only_a_plain_css_length_survives(self):
		"""The value is echoed into a style attribute. Anything that is not a length is DROPPED — not
		escaped and kept, because there is no reason for it to be there at all."""
		smartview.set_column_widths(self.view, {
			self.keys[0]: "12rem",
			self.keys[1]: "12rem; background:url(javascript:alert(1))",
			self.keys[2]: "expression(alert(1))",
		})
		self.assertEqual(self._stored(), {self.keys[0]: "12rem"})

	def test_a_key_the_view_does_not_project_is_dropped(self):
		"""Otherwise this write would quietly become a second, unvalidated column set."""
		smartview.set_column_widths(self.view, {"lead:not_a_column_here": "12rem"})
		self.assertEqual(self._stored(), {})

	def test_it_does_not_look_like_an_edit(self):
		"""A preference is not a definition change: every drag would otherwise land in the audit trail."""
		before = frappe.db.get_value("CRM Smart View", self.view, "modified")
		smartview.set_column_widths(self.view, {self.keys[0]: "18rem"})
		self.assertEqual(frappe.db.get_value("CRM Smart View", self.view, "modified"), before)

	def test_a_caller_who_may_not_write_the_view_stores_nothing(self):
		"""Gated by the SAME rule as every other write here, and refused QUIETLY — a rep dragging a
		column on a shared view keeps it for their session instead of being shown an error."""
		frappe.set_user("Administrator")
		user = "zz-width-reader@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "Width Reader",
			                "send_welcome_email": 0, "roles": [{"role": "Sales User"}]}
			               ).insert(ignore_permissions=True)
			frappe.db.commit()
		try:
			frappe.set_user(user)
			out = smartview.set_column_widths(self.view, {self.keys[0]: "18rem"})
			self.assertEqual(out, {"saved": False})
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(self._stored(), {}, "a caller who cannot write the view wrote to it")
		frappe.delete_doc("User", user, force=True, ignore_permissions=True)
