# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A predicate group joined by OR widens; the same group joined by AND narrows.

`_predicate_where` has always read the group's `op` — `(crit | p) if joiner == "or" else (crit & p)` —
but nothing exercised the OR branch, and the authoring UI could not produce one: the connector between
rows was rendered as static text, so every view anyone built was AND-only. The builder now offers the
choice, which makes this the contract worth holding: the word the author picks has to reach SQL.

The discriminator needs no fixture data. Two conditions on the SAME field for two DIFFERENT values are
mutually exclusive by construction, so AND must return nothing and OR must return each value's rows —
true on any site with two distinct values in one filterable field.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_a_group_can_be_ored
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as sv_api

GRAIN = ("Goodflip-Care", "Anaya", "Nivolumab")


class TestAGroupCanBeOred(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# `upsert_view` creates only when no name is given — it mints one — and updates by that name after.
		# A grain is carried because a grain-less Lead view cannot currently be read at all (F6).
		created = sv_api.upsert_view({
			"label": "ZZ OR Group", "base_object": "Lead",
			"vertical": GRAIN[0], "group": GRAIN[1], "program": GRAIN[2],
		})
		cls.view = created if isinstance(created, str) else (created or {}).get("name")
		cls.field, cls.a, cls.b = cls._two_values()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		if getattr(cls, "view", None) and frappe.db.exists("CRM Smart View", cls.view):
			frappe.delete_doc("CRM Smart View", cls.view, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _two_values(cls):
		"""A column holding two distinct values, read off the ENGINE'S OWN ROWS rather than the table.

		Asking the catalog for a `fieldname` and then reading that column off `tabCRM Lead` does not work
		and should not: a catalog key is not a parent column (it may live on a child table or be derived),
		which is the whole reason `sql_source` exists. Taking the values from a page the view itself
		returned guarantees both that the key is real and that the values are reachable by a filter."""
		filterable = {r["field_key"] for r in (sv_api.field_catalog(base_object="Lead") or []) if r.get("filterable")}
		rows = sv_api.get_data(view=cls.view, page=1, page_size=50, with_count=0).get("rows") or []
		for key in sorted(filterable):
			vals = sorted({
				r.get(key) for r in rows
				# A scalar only: a JSON-ish cell (an assignee list) is not something `=` compares.
				if isinstance(r.get(key), str) and r.get(key) and not r[key].startswith(("[", "{"))
			})
			if len(vals) >= 2:
				return key, vals[0], vals[1]
		return None, None, None

	def _total(self, op):
		sv_api.upsert_view({
			"name": self.view,
			"label": "ZZ OR Group",
			"base_object": "Lead",
			"vertical": GRAIN[0], "group": GRAIN[1], "program": GRAIN[2],
			"predicate": {
				"op": op,
				"conditions": [
					{"field": self.field, "operator": "=", "value": self.a},
					{"field": self.field, "operator": "=", "value": self.b},
				],
			},
		})
		return sv_api.get_data(view=self.view, page=1, page_size=1, with_count=1)["total"]

	def test_and_excludes_what_or_includes(self):
		if not self.field:
			self.skipTest("no filterable catalog field with two distinct values on this site")
		self.assertEqual(self._total("and"), 0, "one field cannot equal two values at once")
		self.assertGreater(self._total("or"), 0, "OR must admit the rows AND excluded")

	def test_an_absent_op_still_means_and(self):
		"""A predicate saved before the joiner existed keeps its meaning — the server's own default."""
		if not self.field:
			self.skipTest("no filterable catalog field with two distinct values on this site")
		sv_api.upsert_view({
			"name": self.view,
			"label": "ZZ OR Group",
			"base_object": "Lead",
			"vertical": GRAIN[0], "group": GRAIN[1], "program": GRAIN[2],
			"predicate": {
				"conditions": [
					{"field": self.field, "operator": "=", "value": self.a},
					{"field": self.field, "operator": "=", "value": self.b},
				],
			},
		})
		self.assertEqual(sv_api.get_data(view=self.view, page=1, page_size=1, with_count=1)["total"], 0)
