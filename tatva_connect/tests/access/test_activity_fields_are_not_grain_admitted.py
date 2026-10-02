# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An activity type's key IS its grain, so its fields are not admitted a second time.
Runs as a rep, because Administrator's ALL_GRAINS skips the admission check entirely."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.smartview import api as smartview
from tatva_connect.smartview import catalog
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ Grain Admit Call"
SCHEMA = [
	{"fieldname": "zz_admit_outcome", "label": "Outcome", "fieldtype": "Data", "target": "status"},
	{"fieldname": "zz_admit_note", "label": "Note", "fieldtype": "Small Text"},
]


class TestActivityFieldsAreNotGrainAdmitted(IntegrationTestCase):
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

	def _fields(self, grains):
		return set(catalog._catalog_fields("Activity", self.type_name, grains))

	def test_a_rep_on_the_types_own_grain_gets_the_types_fields(self):
		"""THE defect. The rep's grain IS the type's grain and they resolved nothing at all."""
		fields = self._fields({self.grain})
		self.assertIn("activity:zz_admit_outcome", fields)
		self.assertIn("activity:zz_admit_note", fields)

	def test_a_system_manager_sees_the_same_set(self):
		"""The two must agree: an admin's ALL_GRAINS short-circuit was the only reason this ever worked."""
		self.assertEqual(
			self._fields({self.grain}),
			self._fields(smartview.entitlement.ALL_GRAINS),
			"the type declares its schema once; who is asking does not change what the type has",
		)

