# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Two input surfaces, gated by the ONE brain.

WHATSAPP (read): the variable picker read raw doc.meta.fields, so a field the lead brain redacts
(uncatalogued, or belonging to another grain) flowed into a template variable and a real send. Fixed:
the picker sources its field SET from lead.detail._select — the same projection the Data tab renders.

INTAKE (build): the form builder's target-field list came from raw get_meta (every column on the
doctype), and the only save check asked "does this column exist?" — never "is it in the brain, for this
form's grain?". Fixed: the list IS the brain, scoped to the section AND the form's grain, so a wrong
field cannot be picked; a save-time backstop on the PARENT catches an API/import write. The gate is
deliberately NOT in the fold — dropping a submitted answer silently is worse than any mapping mistake.

An earlier version of this file asserted the CHILD controller's validate(); that call is never made by
a parent save, so it proved nothing. These drive the real save path.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.api.test_input_hole_gates
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import entitlement
from tatva_connect.api import whatsapp
from tatva_connect.intake import api as intake_api
from tatva_connect.lead import detail


class TestWhatsAppPickerReadsBrain(FrappeTestCase):
	"""The picker's field set is the brain's, not a raw doctype-meta walk."""

	def setUp(self):
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Detail", "last_name": "Probe",
			"mobile_no": "+919999000222",
		}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.delete_doc("CRM Lead", self.lead.name, force=True, ignore_permissions=True)

	def test_empty_brain_projection_surfaces_nothing(self):
		# The lead carries real populated meta fields. If the picker read raw meta it would surface
		# them; with the brain projection empty it must return nothing.
		with patch.object(detail, "_select", return_value={}), \
		     patch("crm.api.whatsapp.validate_access", return_value=None):
			self.assertEqual(whatsapp.get_field_options("CRM Lead", self.lead.name), [])

	def test_only_brain_fields_surface(self):
		row = {"field_key": "lead:first_name", "section": "lead", "fieldname": "first_name",
		       "label": "First Name"}
		with patch.object(detail, "_select", return_value={"lead:first_name": row}), \
		     patch("crm.api.whatsapp.validate_access", return_value=None):
			out = whatsapp.get_field_options("CRM Lead", self.lead.name)
		values = [o["value"] for g in out for o in g["options"]]
		self.assertIn("Detail", values)
		self.assertNotIn("+919999000222", values)  # not in the brain set -> not offered


class TestIntakeTargetListIsGrainScoped(FrappeTestCase):
	"""The builder's list comes from the brain, scoped to the form's grain."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# A grain that actually has an internal contract, taken live — never a hardcoded seed.
		ticks = entitlement._internal_ticks()
		cls.grain = next((g for g, keys in ticks.items() if keys and g[0] and g[1]), None)

	def test_no_grain_yields_no_options(self):
		"""Before a grain is chosen the list is empty — the client tells the operator to pick one."""
		self.assertEqual(intake_api.list_target_fields("lead", None, "", "", ""), [])

	def test_list_is_the_brain_not_raw_meta(self):
		if not self.grain:
			self.skipTest("no internal contract seeded on this site")
		offered = {f["fieldname"] for f in intake_api.list_target_fields("lead", None, *self.grain)}
		self.assertTrue(offered, "a contracted grain must be able to map some lead fields")
		catalogued = set(frappe.get_all("CRM Lead API Field", filters={"section": "lead"}, pluck="fieldname"))
		self.assertTrue(offered <= catalogued, "the list must never offer a field the brain does not declare")
		# raw meta carries far more columns than the brain declares — proof we are not walking meta
		total_meta = len([df for df in frappe.get_meta("CRM Lead").fields
		                  if df.fieldtype not in frappe.model.no_value_fields])
		self.assertLess(len(offered), total_meta)

	def test_every_offered_field_is_in_that_grain(self):
		if not self.grain:
			self.skipTest("no internal contract seeded on this site")
		for f in intake_api.list_target_fields("lead", None, *self.grain):
			key = frappe.db.get_value(
				"CRM Lead API Field", {"section": "lead", "fieldname": f["fieldname"]}, "field_key"
			)
			self.assertTrue(entitlement.field_in_grains_via_contract(key, [self.grain]))

	def test_foreign_grain_offers_nothing_of_ours(self):
		self.assertEqual(intake_api.list_target_fields("lead", None, "ZZ No Vertical", "ZZ No Group", "ZZ"), [])


class TestIntakeSaveBackstop(FrappeTestCase):
	"""The save-time gate lives on the PARENT — the only controller a parent save actually runs."""

	def _form(self, grain, target_field):
		return frappe.get_doc({
			"doctype": "CRM Intake Form", "form_name": "ZZ Gate Probe", "enabled": 0,
			"custom_vertical": grain[0], "custom_group": grain[1], "custom_current_program": grain[2],
			"mappings": [{"source_field": "phone", "fieldtype": "Phone", "target_table": "lead",
			              "target_field": target_field}],
		})

	def test_uncatalogued_target_is_rejected_on_save(self):
		ticks = entitlement._internal_ticks()
		grain = next((g for g, keys in ticks.items() if keys and g[0] and g[1]), None)
		if not grain:
			self.skipTest("no internal contract seeded on this site")
		# A REAL CRM Lead column that the brain does not declare — the case the old has_field check passed.
		uncatalogued = next(
			(df.fieldname for df in frappe.get_meta("CRM Lead").fields
			 if df.fieldtype not in frappe.model.no_value_fields
			 and not frappe.db.exists("CRM Lead API Field", {"section": "lead", "fieldname": df.fieldname})),
			None,
		)
		if not uncatalogued:
			self.skipTest("every CRM Lead column is catalogued on this site")
		with self.assertRaises(frappe.ValidationError):
			self._form(grain, uncatalogued).validate()

	def test_grain_must_be_set_before_mapping(self):
		with self.assertRaises(frappe.ValidationError):
			self._form(("", "", ""), "mobile_no").validate()
