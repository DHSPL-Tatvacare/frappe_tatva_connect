# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A child column a view only DISPLAYS must not put a join in the page query.

Measured on UAT (117,529 tasks, 56,745 `CRM Task Engagement` rows), asking for ONE child column:

    task column only ................................. 0.6s
    + the child column, ranked derived table ......... 30s, one CPU at 100%
    + the child column, plain indexed join ........... killed at 10s
    page resolved first, then the child read ......... instant

MariaDB refused the first two outright with MAX_JOIN_SIZE. The page query cannot carry a join to a
child table at this size: `LIMIT 50` is applied AFTER the join, so the whole table is walked to return
fifty rows, and the cost grows with the data instead of with the page.

`_hydrate` already solves this for key-value answers — fetch the page, then read `parent IN (the page)`.
Child columns were excluded from it deliberately, on the reasoning that a section of real columns is one
join and moving it "would buy nothing". The measurements above are what that reasoning cost.

So this pins the rule in both directions, because the perf fix is only safe if the narrowing half still
works: a child column that is merely PROJECTED leaves the query; one that is FILTERED or SORTED stays in
it, because you cannot page a list before you have narrowed it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_child_columns_do_not_join
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from tatva_connect.smartview import api as smartview
from tatva_connect.tests.api import partner_fixture

FIELD = "lab:report_date"
PHONE_TWO_ROWS = "+916100070001"
PHONE_ONE_ROW = "+916100070002"


class TestChildColumnsDoNotJoin(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		partner_fixture.mint_grain()
		cls.contract = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": "ZZ Child Hydrate Contract", "enabled": 1,
			"is_internal": 1, "vertical": partner_fixture.VERTICAL, "crm_group": partner_fixture.GROUP,
			"allowed_fields": [{"field": FIELD}],
		}).insert(ignore_permissions=True).name

		cls.old_date = add_days(nowdate(), -200)
		cls.new_date = add_days(nowdate(), -2)
		# Two rows on ONE lead: the flattened cell must read the LATEST, which is the whole reason the
		# ranking existed in the query. Moving the read must not quietly change which row wins.
		cls.two = cls._lead(PHONE_TWO_ROWS, [cls.old_date, cls.new_date])
		cls.one = cls._lead(PHONE_ONE_ROW, [cls.old_date])

		cls.view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": "ZZ Child Hydrate View", "base_object": "Lead",
			"is_standard": 1, "vertical": partner_fixture.VERTICAL, "group": partner_fixture.GROUP,
			"columns": frappe.as_json([FIELD]),
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for name in frappe.get_all("CRM Smart View", filters={"label": "ZZ Child Hydrate View"}, pluck="name"):
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)
		if frappe.db.exists("CRM Lead API Mapping", cls.contract):
			frappe.delete_doc("CRM Lead API Mapping", cls.contract, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610007%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	@classmethod
	def _lead(cls, phone, report_dates):
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Child Hydrate", "mobile_no": phone, "status": "New",
			"custom_vertical": partner_fixture.VERTICAL, "custom_group": partner_fixture.GROUP,
			"custom_lab_profile": [{"report_date": d} for d in report_dates],
		}).insert(ignore_permissions=True).name

	def _cat(self):
		return smartview._catalog_fields("Lead", None, [(partner_fixture.VERTICAL, partner_fixture.GROUP, "")], frappe.get_roles())

	# ---- the perf invariant -------------------------------------------------

	def test_a_projected_child_column_leaves_the_page_query(self):
		"""THE defect. A displayed-only child column joined the page query, and that join is what walked
		the whole table to return fifty rows. It must be read after the page instead."""
		cat = self._cat()
		self.assertIn(FIELD, cat, "the fixture's contract must grant the field, or this proves nothing")
		self.assertEqual(cat[FIELD].sql_source, "child", "the field under test has to BE a child column")
		moved = smartview._hydrate_split({FIELD}, must_query=set(), cat=cat)
		self.assertIn(FIELD, moved, "a projected-only child column must not reach the query")

	def test_a_filtered_child_column_stays_in_the_page_query(self):
		"""The other half, and the one that makes the fix safe: you cannot page a list before you have
		narrowed it, so a column that decides WHICH rows must remain in the SQL."""
		cat = self._cat()
		moved = smartview._hydrate_split({FIELD}, must_query={FIELD}, cat=cat)
		self.assertNotIn(FIELD, moved, "a filtered or sorted child column must stay in the query")

	# ---- the behaviour that must not change ---------------------------------

	def test_a_multi_row_child_still_reads_its_latest_row(self):
		"""The ranking the query used to do lives in the read now. A lead with two lab rows must still
		flatten to the NEWEST one — the rule every other consumer of a multi-row section reads by."""
		rows = {r["name"]: r for r in smartview.get_data(self.view, page_size=200)["rows"]}
		self.assertIn(self.two, rows, "the lead must be on the page at all")
		self.assertEqual(
			str(rows[self.two][FIELD])[:10], self.new_date,
			"the flattened cell must be the latest lab row, not the oldest",
		)

	def test_a_single_row_child_reads_its_only_row(self):
		"""The ordinary case: one row, and its value is the cell."""
		rows = {r["name"]: r for r in smartview.get_data(self.view, page_size=200)["rows"]}
		self.assertEqual(str(rows[self.one][FIELD])[:10], self.old_date)

	def test_every_page_row_carries_the_column_even_when_it_has_no_value(self):
		"""A lead with no lab row must still carry the key, or a grid binding it renders broken rather
		than blank — the guarantee `_hydrate` already made for answers."""
		bare = self._lead("+916100070003", [])
		frappe.db.commit()
		rows = {r["name"]: r for r in smartview.get_data(self.view, page_size=200)["rows"]}
		self.assertIn(FIELD, rows[bare], "the key must be present")
		self.assertIsNone(rows[bare][FIELD], "and absent means None, not missing")

	def test_filtering_on_the_child_column_still_narrows(self):
		"""End to end: the narrowing path is untouched by the move."""
		out = smartview.get_data(
			self.view,
			filters=frappe.as_json([[FIELD, "between", [add_days(nowdate(), -7), nowdate()]]]),
			page_size=200,
		)
		names = {r["name"] for r in out["rows"]}
		self.assertIn(self.two, names, "the lead with a recent lab row is selected")
		self.assertNotIn(self.one, names, "the lead with only an old one is not")
