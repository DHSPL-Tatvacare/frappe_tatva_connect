# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One Create Lead layout, and the picked grain decides which sections are drawn — TatvaPractice gets the doctor block, and the gate subtracts from nobody else."""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.lead import quick_entry

LAYOUT = "CRM Lead-Quick Entry"

# The block under test and the only grain whose contract ticks it.
DOCTOR_FIELDS = [
	"custom_clinic_or_hospital_name", "custom_type_of_clinic", "custom_doctor_speciality",
	"custom_doctor_age", "custom_location_strata",
	"custom_address_line_1", "custom_address_line_2", "custom_country",
]
TP = ("Tatvapractice", "India", "Field-Sales")
GOODFLIP = ("Goodflip", "India", "Inside-Sales")


def _visible(tabs):
	"""Every fieldname the form actually draws — a hidden section's fields are not on the form."""
	shown = set()
	for tab in tabs:
		for section in tab.get("sections") or []:
			if section.get("hidden"):
				continue
			for column in section.get("columns") or []:
				for field in column.get("fields") or []:
					if isinstance(field, dict) and not field.get("hidden"):
						shown.add(field["fieldname"])
	return shown


class TestQuickEntryGrainSections(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# A System Manager keeps the axis fields, so the only difference between the cases below is the grain.
		frappe.set_user("Administrator")
		cls.authored = _visible(quick_entry.get_fields_layout("CRM Lead", "Quick Entry"))

	def test_the_doctor_block_is_drawn_for_tatvapractice(self):
		shown = _visible(quick_entry.get_fields_layout("CRM Lead", "Quick Entry", None, *TP))
		missing = [f for f in DOCTOR_FIELDS if f not in shown]
		self.assertFalse(missing, f"TatvaPractice must get its doctor fields; missing {missing}")

	def test_no_other_business_sees_it(self):
		shown = _visible(quick_entry.get_fields_layout("CRM Lead", "Quick Entry", None, *GOODFLIP))
		leaked = [f for f in DOCTOR_FIELDS if f in shown]
		self.assertFalse(leaked, f"Goodflip must not see TatvaPractice's doctor fields; got {leaked}")

	def test_it_takes_nothing_away_from_the_business_it_is_not_for(self):
		"""The whole risk of this change, in one assertion: Goodflip's form loses NOT ONE field."""
		shown = _visible(quick_entry.get_fields_layout("CRM Lead", "Quick Entry", None, *GOODFLIP))
		lost = [f for f in self.authored - set(DOCTOR_FIELDS) if f not in shown]
		self.assertFalse(lost, f"Goodflip's Create Lead form lost {lost}")

	def test_an_uncovered_grain_gets_the_whole_form_not_an_empty_one(self):
		"""Fail open: nothing declares this leaf, so there is nothing to filter by — and filtering on an empty answer draws nothing."""
		shown = _visible(quick_entry.get_fields_layout(
			"CRM Lead", "Quick Entry", None, "Nowhere", "Nobody", "Nothing"))
		self.assertEqual(shown, self.authored)

	def test_the_required_tick_is_the_contracts_to_give(self):
		"""`reqd` on this form is read off the leaf's contract, and only an internal one carries it."""
		field = frappe.db.get_value(
			"CRM Lead API Mapping Field",
			{"parent": "Tatvapractice::India::Field-Sales::Internal Visibility",
			 "field": "lead:custom_clinic_or_hospital_name"}, "name")
		if not field:
			self.skipTest("TatvaPractice's contract does not tick the clinic name on this site")
		frappe.db.set_value("CRM Lead API Mapping Field", field, "reqd_on_create_form", 1)
		setattr(frappe.local, entitlement._INTERNAL_REQUIRED_CACHE, None)
		try:
			tabs = quick_entry.get_fields_layout("CRM Lead", "Quick Entry", None, *TP)
			starred = {
				f["fieldname"]
				for tab in tabs for s in tab.get("sections") or [] for c in s.get("columns") or []
				for f in c.get("fields") or [] if isinstance(f, dict) and f.get("reqd")
			}
			self.assertIn("custom_clinic_or_hospital_name", starred)
		finally:
			frappe.db.set_value("CRM Lead API Mapping Field", field, "reqd_on_create_form", 0)
			frappe.db.rollback()
