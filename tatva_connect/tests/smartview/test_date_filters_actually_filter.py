# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A date filter filters. Today every one of them is silently discarded.

`Filter.vue`'s `getDefaultOperator` returns **`between`** for every Date and Datetime field, so a date
filter arrives as `between` the instant a user adds one. The bridge that turns the control's emit into a
composer predicate then does:

    else:
        continue  # between / timespan — the composer can't express these yet

The condition vanishes. The list does not change, the count does not change, and nothing anywhere says
why. It is the same fail-open shape as the row gate and the column fallback: a thing the code cannot do,
absorbed in silence instead of refused or supported.

`between` and `timespan` are the two operators the control offers for a date and the composer does not
carry. Frappe already resolves a timespan — `frappe.utils.get_timespan_date_range` — and its option
values are the exact strings the control emits ("last week", "last month", …), so nothing here computes
a date range of its own.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_date_filters_actually_filter
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from tatva_connect.smartview import api as smartview
from tatva_connect.tests.api import partner_fixture

FIELD = "lab:report_date"
PHONE_OLD = "+916100060001"
PHONE_RECENT = "+916100060002"


class TestDateFiltersActuallyFilter(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		partner_fixture.mint_grain()
		# The grain must actually GRANT the field, or the catalog excludes it and the filter is refused
		# before any operator is reached — the fixture would then prove nothing about dates.
		cls.contract = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": "ZZ Date Filter Contract", "enabled": 1,
			"is_internal": 1, "vertical": partner_fixture.VERTICAL, "crm_group": partner_fixture.GROUP,
			"allowed_fields": [{"field": FIELD}],
		}).insert(ignore_permissions=True).name
		cls.old_date = add_days(nowdate(), -200)
		cls.recent_date = add_days(nowdate(), -2)
		cls.old = cls._lead(PHONE_OLD, cls.old_date)
		cls.recent = cls._lead(PHONE_RECENT, cls.recent_date)
		cls.view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": "ZZ Date Filter View", "base_object": "Lead",
			"is_standard": 1, "vertical": partner_fixture.VERTICAL, "group": partner_fixture.GROUP,
			"columns": frappe.as_json([FIELD]),
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		for name in frappe.get_all("CRM Smart View", filters={"label": "ZZ Date Filter View"}, pluck="name"):
			frappe.delete_doc("CRM Smart View", name, force=True, ignore_permissions=True)
		if frappe.db.exists("CRM Lead API Mapping", cls.contract):
			frappe.delete_doc("CRM Lead API Mapping", cls.contract, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610006%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	@classmethod
	def _lead(cls, phone, report_date):
		doc = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Date Filter", "mobile_no": phone, "status": "New",
			"custom_vertical": partner_fixture.VERTICAL, "custom_group": partner_fixture.GROUP,
			"custom_lab_profile": [{"report_date": report_date}],
		}).insert(ignore_permissions=True)
		return doc.name

	def _names(self, condition):
		rows = smartview.get_data(self.view, filters=frappe.as_json([condition]), page_size=200)
		return {r["name"] for r in rows["rows"]}

	def test_between_selects_only_the_rows_inside_the_range(self):
		"""THE defect. `between` is what the control sends for EVERY date field by default, and it was
		dropped — so the list came back identical whatever range the user chose."""
		names = self._names([FIELD, "between", [add_days(nowdate(), -7), nowdate()]])
		self.assertIn(self.recent, names, "a lead inside the range must be selected")
		self.assertNotIn(self.old, names, "a lead outside it must not")

	def test_between_accepts_the_comma_string_the_picker_actually_emits(self):
		"""The shape the UI sends: DateRangePicker emits "from,to" as ONE string, which the list-only fixture never exercised."""
		names = self._names([FIELD, "between", f"{add_days(nowdate(), -7)},{nowdate()}"])
		self.assertIn(self.recent, names, "a lead inside the range must be selected")
		self.assertNotIn(self.old, names, "a lead outside it must not")

	def test_between_is_inclusive_of_its_bounds(self):
		"""A range that is exactly the row's own date must contain it — an exclusive bound would hide the
		row a user picked the date from."""
		names = self._names([FIELD, "between", [self.recent_date, self.recent_date]])
		self.assertIn(self.recent, names)

	def test_a_timespan_resolves_through_frappes_own_range(self):
		"""The control's values ("last week", "last 6 months", …) are frappe's own timespan strings, so the
		range comes from `get_timespan_date_range` and no date arithmetic is written here.

		Note the semantics that resolver gives them: they are CALENDAR-relative, not rolling. "last 6
		months" is the six whole months before this one, so it holds the older lead and excludes the one
		from two days ago. Asserting it the other way round would be asserting a rolling window frappe
		does not implement."""
		names = self._names([FIELD, "timespan", "last 6 months"])
		self.assertIn(self.old, names, "a lead inside the resolved calendar range is selected")
		self.assertNotIn(self.recent, names, "one outside it is not")

	def test_an_operator_the_composer_cannot_run_is_refused_not_ignored(self):
		"""The rule this file exists for: a filter that cannot be honoured must SAY so. Silently dropping
		it is how a user ends up trusting a list that never applied what they asked for."""
		with self.assertRaises(frappe.ValidationError):
			self._names([FIELD, "sounds like", "x"])
