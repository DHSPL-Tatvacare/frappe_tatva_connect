# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A stage picker offers the stages on YOUR leads, and a standard column can reach the quick-filter bar.

TWO DEFECTS, BOTH FOUND ON PROD.

STAGE WAS NEVER SCOPED. `grain_filter_options` reads the values present on rows the caller can see, and
which fields it covers was `field.options in GRAIN_MASTERS` — the three masters that ARE a grain. But
`CRM Lead Stage` is keyed BY one: it holds `Inside-Sales::New Patient` beside `Nivolumab::New Patient`.
So it got `link_query`, which de-duplicates those keys into labels but reads the whole master, and never
got the scoping. Measured for `shantha.s@tatvacare.in` (Sales Manager, Goodflip / India / Inside-Sales):
the picker offered 137 labels drawn from all eight programmes, where their 108,879 visible leads carry
33 labels from one. Not an access hole — the list itself is scoped — but every other line's vocabulary,
and 104 choices that match nothing.

CREATED ON COULD BE CHOSEN AND NEVER APPEARED. `creation` is a frappe standard column with no DocField,
so it is not in `meta.fields`; native resolves each chosen quick-filter name against that list and drops
what it cannot find, special-casing only `name`. The Filter menu reads a different source and shows it,
which is why it looked arbitrary.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.lead.test_stage_filters_are_scoped
"""
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import task_lenses
from tatva_connect.lead import filters as lead_filters
from tatva_connect.taxonomy import labels

LEAD = "CRM Lead"
SETTINGS = "CRM Global Settings"
STAGE_FIELD = "custom_substage"


class TestStageFiltersAreScoped(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		row = frappe.db.exists(SETTINGS, {"dt": LEAD, "type": "Quick Filters"})
		cls._row = row
		cls._before = frappe.db.get_value(SETTINGS, row, "json") if row else None

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		if cls._row:
			frappe.db.set_value(SETTINGS, cls._row, "json", cls._before)
		elif frappe.db.exists(SETTINGS, {"dt": LEAD, "type": "Quick Filters"}):
			frappe.db.delete(SETTINGS, {"dt": LEAD, "type": "Quick Filters"})
		frappe.db.commit()
		super().tearDownClass()

	def _choose(self, names):
		if self._row:
			frappe.db.set_value(SETTINGS, self._row, "json", json.dumps(names))
		else:
			frappe.get_doc({"doctype": SETTINGS, "dt": LEAD, "type": "Quick Filters",
			                "json": json.dumps(names)}).insert(ignore_permissions=True)
		frappe.db.commit()
		frappe.cache.delete_keys("")

	def test_a_stage_link_is_an_axis(self):
		"""The rule that was missing, stated where it is decided rather than at a fieldname."""
		self.assertIn(STAGE_FIELD, lead_filters.grain_filter_fields(LEAD))
		self.assertTrue(lead_filters.is_axis("CRM Lead Stage"))

	def test_a_picklist_link_is_left_to_its_own_scoping(self):
		"""`CRM Picklist Value` is composite too and already scoped twice over, by `picklist_query` and its
		own permission_query_conditions. Claiming it here would replace a working type-ahead with a stamped
		Select and hide the control the moment a caller's leads use none of its values."""
		self.assertFalse(lead_filters.is_axis("CRM Picklist Value"))

	def test_the_stage_options_are_labels_not_composite_keys(self):
		"""The control offers labels and `filter_on` reads a label back as every key it means, so a key
		stamped here would show `Inside-Sales::New Patient` and filter on something never offered."""
		offered = lead_filters.grain_filter_options(LEAD).get(STAGE_FIELD) or []
		self.assertTrue(all("::" not in value for value in offered),
		                f"a composite key reached the picker: {offered[:3]}")

	def test_the_stage_options_are_only_what_the_callers_leads_carry(self):
		"""THE defect. Scoped, so it can never be the whole master — that is the leak this closes."""
		offered = set(lead_filters.grain_filter_options(LEAD).get(STAGE_FIELD) or [])
		visible = frappe.get_list(LEAD, fields=[STAGE_FIELD], distinct=True,
		                          limit_page_length=0, ignore_ifnull=True)
		carried = {labels.title_of("CRM Lead Stage", r.get(STAGE_FIELD)) or r.get(STAGE_FIELD)
		           for r in visible if r.get(STAGE_FIELD)}
		self.assertEqual(offered, carried, "the picker and the caller's own leads disagree")
		self.assertLessEqual(offered, set(labels.labels_of("CRM Lead Stage")),
		                     "the picker offered a label the master does not hold")

	def test_every_axis_costs_ONE_scan_however_many_there_are(self):
		"""Each axis asked the SAME question of the same rows, and the row gate is a disjunction no index
		can narrow — so N axes were N full scans of the biggest table in the app. Distinct COMBINATIONS is
		the same answer in one pass. Measured on prod: 7 x 212ms -> 155ms.

		Only the SCAN is counted. Naming the labels is `labels.labels`, which answers from the doc cache —
		N reads once and nothing thereafter — so asserting a read count there would pin a cold cache and
		fail on a warm one."""
		seen = []
		original = frappe.db.__class__.sql

		def spy(self, query, *args, **kwargs):
			seen.append(" ".join(str(query).split()))
			return original(self, query, *args, **kwargs)

		frappe.cache.delete_keys("")
		frappe.db.__class__.sql = spy
		try:
			lead_filters.grain_filter_options(LEAD)
		finally:
			frappe.db.__class__.sql = original

		scans = [q for q in seen if "DISTINCT" in q.upper() and f"tab{LEAD}" in q]
		self.assertEqual(len(scans), 1, f"{len(lead_filters.grain_filter_fields(LEAD))} axes cost {len(scans)} scans")

	def test_a_standard_column_reaches_the_quick_filter_bar(self):
		"""`creation` has no DocField, so native drops it silently. Resolved from `frappe.model.std_fields`,
		which is where frappe already says what that column is."""
		self._choose(["mobile_no", "creation"])
		bar = {f["fieldname"]: f for f in task_lenses.get_quick_filters(LEAD)}
		self.assertIn("creation", bar, "Created On can be chosen and still never appears")
		self.assertEqual(bar["creation"]["label"], "Created On")
		self.assertEqual(bar["creation"]["fieldtype"], "Datetime")

	def test_a_name_nothing_can_answer_for_is_dropped_not_guessed(self):
		"""A stored choice can outlive the field it names; that is a shorter bar, never a broken one."""
		self._choose(["mobile_no", "zz_no_such_field"])
		names = [f["fieldname"] for f in task_lenses.get_quick_filters(LEAD)]
		self.assertEqual(names, ["mobile_no"])

	def test_adding_a_standard_column_stamps_no_property_setter(self):
		"""The write half. Native writes `<doctype>-<field>-in_standard_filter` for everything it is handed,
		and `make_property_setter(..., validate_fields_for_doctype=False)` does not refuse a name with no
		DocField — so it would happily describe a column that does not exist. Withheld by the same rule that
		withholds a derived name, asked of the meta rather than kept as a list."""
		prop = {"doc_type": LEAD, "field_name": "creation", "property": "in_standard_filter"}
		frappe.db.delete("Property Setter", prop)
		frappe.db.commit()

		task_lenses.update_quick_filters(json.dumps(["mobile_no", "creation"]), json.dumps(["mobile_no"]), LEAD)

		self.assertFalse(frappe.db.exists("Property Setter", prop),
		                 "a Property Setter was stamped for a column that has no DocField")
		stored = frappe.parse_json(frappe.db.get_value(SETTINGS, {"dt": LEAD, "type": "Quick Filters"}, "json"))
		self.assertIn("creation", stored, "the rep's choice must still be recorded")

	def test_a_real_fieldname_still_gets_native_behaviour(self):
		"""The withhold must not change what native does for an ordinary column."""
		prop = {"doc_type": LEAD, "field_name": "source", "property": "in_standard_filter"}
		frappe.db.delete("Property Setter", prop)
		frappe.db.commit()

		task_lenses.update_quick_filters(json.dumps(["mobile_no", "source"]), json.dumps(["mobile_no"]), LEAD)

		self.assertTrue(frappe.db.exists("Property Setter", prop),
		                "a real fieldname lost native's Property Setter write")
		frappe.db.delete("Property Setter", prop)
		frappe.db.commit()
