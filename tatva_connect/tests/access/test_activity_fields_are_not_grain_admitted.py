# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An activity type's key IS its grain, so its fields are not admitted a second time.

`_catalog_fields` says so in its own docstring — *"the type's key already carries its grain, so its
fields need no second grain filter — resolve_fields still applies the role restrictions"* — and then
calls `resolve_fields`, which admits a key only when an internal contract ticks it. Contracts tick LEAD
keys; an activity key is `activity:<fieldname>` and no contract has ever carried one. So every
non-System-Manager resolved ZERO activity fields, whatever grain they held.

Measured before the fix, on live types: `brain=35 sysmgr=35 rep=0` — the rep's grain matching the
type's exactly. Downstream that is not a thin list, it is a dead surface: no columns, a saved predicate
falling to `1=0` and returning no rows, and `_validate_columns` throwing on any key the picker offers.

Invisible to the suite because every other activity test runs as Administrator, whose ALL_GRAINS
short-circuits the admission before it is ever asked.

NOT asserted here, because the data model cannot express it: `CRM Lead Field Restriction.field` is a Link
to `CRM Lead API Field`, so an ACTIVITY field cannot be named in a restriction at all. The restriction
half is kept on this path regardless — it is the correct question to ask, and it becomes answerable the
day that Link widens.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_activity_fields_are_not_grain_admitted
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as smartview
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ Grain Admit Call"
SCHEMA = [
	{"fieldname": "zz_admit_outcome", "label": "Outcome", "fieldtype": "Data", "target": "status"},
	{"fieldname": "zz_admit_note", "label": "Note", "fieldtype": "Small Text"},
]


class TestActivityFieldsAreNotGrainAdmitted(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.type_name = task_type_fixture.mint_type(TYPE_NAME, SCHEMA)
		cls.grain = (task_type_fixture.VERTICAL, task_type_fixture.GROUP, "")

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	def _fields(self, grains, roles):
		return set(smartview._catalog_fields("Activity", self.type_name, grains, roles))

	def test_a_rep_on_the_types_own_grain_gets_the_types_fields(self):
		"""THE defect. The rep's grain IS the type's grain and they resolved nothing at all."""
		fields = self._fields({self.grain}, ["Sales User"])
		self.assertIn("activity:zz_admit_outcome", fields)
		self.assertIn("activity:zz_admit_note", fields)

	def test_a_system_manager_sees_the_same_set(self):
		"""The two must agree: an admin's ALL_GRAINS short-circuit was the only reason this ever worked."""
		self.assertEqual(
			self._fields({self.grain}, ["Sales User"]),
			self._fields(smartview.entitlement.ALL_GRAINS, ["System Manager"]),
			"the type declares its schema once; who is asking does not change what the type has",
		)

	def test_the_type_is_still_reached_through_its_own_grain(self):
		"""Not grain-filtering the FIELDS is not the same as unscoping the TYPE. A type is reached through
		the view's grain, and a caller not entitled to that grain never gets this far — `_grains_from_axes`
		refuses it. This asserts the schema is intact, not that anyone may ask for it."""
		self.assertTrue(self._fields({self.grain}, ["Sales User"]), "the type's schema must resolve")
