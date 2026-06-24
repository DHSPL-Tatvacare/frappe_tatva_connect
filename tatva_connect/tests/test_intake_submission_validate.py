# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Phase 1 — server-side validation on CRM Enrolment Submission.

Proves the controller's fail-closed guards hold regardless of the (advisory) web-form
client rules: a bad phone, an out-of-state city, an out-of-grain hospital, or a doctor
not at the chosen hospital are all rejected; a clean in-grain submission saves.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

V, G, P = "GoodFlip Care", "Anaya", "Nivolumab"
FORM = "TEST Niva Enrolment"
STATE, OTHER_STATE = "Karnataka", "Maharashtra"


def _ensure(doctype, name, **fields):
	if not frappe.db.exists(doctype, name):
		frappe.get_doc(dict(doctype=doctype, **fields)).insert(ignore_permissions=True)


class TestIntakeSubmissionValidate(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_ensure("CRM Vertical", V, vertical_name=V)
		_ensure("CRM Group", G, group_name=G)
		_ensure("CRM Program", P, program_name=P)
		# City in Karnataka (composite key mirrors prod: "City::State")
		cls.city = "Bengaluru::Karnataka"
		_ensure("CRM City", cls.city, city_name="Bengaluru", state=STATE)
		# Hospital in grain; Doctor at that hospital
		cls.hosp = "{0}::{1}::{2}::Apollo".format(V, G, P)
		_ensure("CRM Hospital", cls.hosp, hospital_name="Apollo", vertical=V, group=G, program=P)
		cls.doc_doctor = "{0}::Dr A".format(cls.hosp)
		_ensure("CRM Doctor", cls.doc_doctor, doctor_name="Dr A", hospital=cls.hosp)
		# Out-of-grain hospital (different program) to prove the grain check bites
		cls.hosp_bad = "{0}::{1}::OtherProg::Fortis".format(V, G)
		_ensure("CRM Hospital", cls.hosp_bad, hospital_name="Fortis", vertical=V, group=G, program="OtherProg")
		if not frappe.db.exists("CRM Intake Form", FORM):
			frappe.get_doc({
				"doctype": "CRM Intake Form", "form_name": FORM, "enabled": 1,
				"custom_vertical": V, "custom_group": G, "custom_current_program": P,
				"mappings": [{"source_field": "patient_name", "target_table": "lead", "target_field": "first_name"}],
			}).insert(ignore_permissions=True)

	def _sub(self, **over):
		base = dict(
			doctype="CRM Enrolment Submission", intake_form=FORM, patient_name="X",
			phone="9876543210", state=STATE, city=self.city,
			nivolumab_dosage="100 mg", nivolumab_indication="NSCLC", remarks="r",
		)
		base.update(over)
		return frappe.get_doc(base)

	def test_clean_submission_validates(self):
		self._sub(hospital=self.hosp, doctor=self.doc_doctor).validate()  # no throw

	def test_bad_phone_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._sub(phone="123").validate()

	def test_city_out_of_state_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._sub(state=OTHER_STATE).validate()

	def test_hospital_out_of_grain_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._sub(hospital=self.hosp_bad).validate()

	def test_doctor_not_at_hospital_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._sub(hospital=self.hosp, doctor="{0}::Ghost".format(self.hosp_bad)).validate()

	def test_not_listed_skips_scope_check(self):
		# Operator typed a hospital not in the master -> manual path, grain check skipped.
		self._sub(hospital_not_listed=1, hospital_manual="New Clinic").validate()  # no throw
