# Copyright (c) 2026, TatvaCare and Contributors. See license.txt
"""A negative filter keeps the blank cells the native list keeps: `F = a` and `F != a` partition the view."""
import frappe
from frappe.query_builder.functions import Count
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as sv_api
from tatva_connect.smartview import catalog

GRAIN = ("Goodflip-Care", "Anaya", "")


class TestBlankCellsCompareNatively(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		created = sv_api.upsert_view({"label": "ZZ Blank Cells", "base_object": "Lead", "vertical": GRAIN[0], "group": GRAIN[1], "program": GRAIN[2]})
		cls.view = created if isinstance(created, str) else (created or {}).get("name")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		if getattr(cls, "view", None) and frappe.db.exists("CRM Smart View", cls.view):
			frappe.delete_doc("CRM Smart View", cls.view, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _total(self, *filters):
		return sv_api.get_data(view=self.view, filters=frappe.as_json(list(filters)), page_size=1)["total"]

	def _field_with_blanks(self):
		"""A filterable driving-row Data/Select key holding both a value and a real NULL, so a NULL-dropping `!=` goes red."""
		cat = catalog._lead_catalog()
		lead = frappe.qb.DocType("CRM Lead")
		for key in sorted(k for k, r in cat.items() if r.filterable and r.sql_source == "parent"):
			if catalog._col_type(cat[key])[0] not in ("Data", "Select"):
				continue
			fieldname = cat[key].fieldname
			value = frappe.db.get_value("CRM Lead", {fieldname: ["is", "set"]}, fieldname)
			if value and frappe.qb.from_(lead).select(Count("*")).where(lead[fieldname].isnull()).run()[0][0]:
				return key, value
		return None, None

	def test_equal_and_not_equal_partition_the_view(self):
		key, value = self._field_with_blanks()
		if not key:
			self.skipTest("no filterable field here holds both a value and a blank")
		whole = sv_api.get_data(view=self.view, page_size=1)["total"]
		self.assertEqual(self._total([key, "=", value]) + self._total([key, "!=", value]), whole, f"{key} != must keep blank rows")

	def test_set_and_not_set_partition_the_view(self):
		key, _value = self._field_with_blanks()
		if not key:
			self.skipTest("no filterable field here holds both a value and a blank")
		whole = sv_api.get_data(view=self.view, page_size=1)["total"]
		self.assertEqual(self._total([key, "is set", None]) + self._total([key, "is not set", None]), whole)
