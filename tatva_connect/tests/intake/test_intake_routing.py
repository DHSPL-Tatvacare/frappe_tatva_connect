# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Web-intake routing reads the ONE brain, `CRM Lead Section` — no private `_TABLE` dict.

Before this phase `_TABLE` (intake.py) omitted `acq` and `metrics`, so an `acq:*` mapping had no
child_table_field to land on and was silently dropped: a live data-loss bug. Routing now comes
straight off the section row (`child_table_field` / no child_table_field), so every section —
including `acq` — routes, and a `note` mapping still works exactly as before (it is not a section).

Reuses the fixture shape `test_intake_builder_spine.py` already proved: a real grain, a real
`CRM Intake Form`, the builder's scaffolded per-form sink, and the wildcard router firing on insert.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import seed
from tatva_connect.intake import builder, intake
from tatva_connect.partner_api import section_seed
from tatva_connect.whatsapp.phone import to_e164

_INTAKE_SWITCH = "Lead::Enrolment::intake"
_DEDUP_SWITCH = "Lead::CRM Lead::dedup"

_VERTICAL = "GoodFlip Care"
_GROUP = "Anaya"
_PROGRAM = "Nivolumab"
_SOURCE = "Enrolment Form"
_FORM = "Routing Test Form"
_PHONE = "+91 9000000008"


class TestIntakeRouting(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
		section_seed.ensure_rows()  # idempotent — the section brain this phase routes on
		self._made = []  # (doctype, name) torn down in reverse
		self._set_switch(_INTAKE_SWITCH, 1)
		self._set_switch(_DEDUP_SWITCH, 1)

		self._ensure("CRM Vertical", _VERTICAL, {"vertical_name": _VERTICAL})
		self._ensure("CRM Group", _GROUP, {"group_name": _GROUP})
		self._ensure("CRM Program", _PROGRAM, {"program_name": _PROGRAM})
		self._ensure("CRM Lead Source", _SOURCE, {"source_name": _SOURCE})

		self.cfg = self._intake_form()
		self.dt = builder.doctype_name_for(self.cfg)

	def tearDown(self):
		frappe.set_user("Administrator")
		for ld in frappe.get_all("CRM Lead", filters={"mobile_no": to_e164(_PHONE)}, pluck="name"):
			self._purge_lead(ld)
		for wf in frappe.get_all("Web Form", filters={"doc_type": self.dt}, pluck="name"):
			frappe.delete_doc("Web Form", wf, force=True, ignore_permissions=True)
		if frappe.db.exists("DocType", self.dt):
			frappe.delete_doc("DocType", self.dt, force=True, ignore_permissions=True)
			frappe.db.delete("Singles", {"doctype": self.dt})
		for dt, name in reversed(self._made):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		intake.bust_intake_doctype_cache()
		self._set_switch(_INTAKE_SWITCH, 0)
		self._set_switch(_DEDUP_SWITCH, 0)

	# --- helpers ---------------------------------------------------------
	def _set_switch(self, key, on):
		if frappe.db.exists("CRM Tatva Automation", key):
			frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1 if on else 0)

	def _ensure(self, doctype, name, values):
		if frappe.db.exists(doctype, name):
			return name
		d = frappe.new_doc(doctype)
		d.update(values)
		d.insert(ignore_permissions=True)
		self._made.append((doctype, d.name))
		return d.name

	def _intake_form(self):
		if frappe.db.exists("CRM Intake Form", _FORM):
			frappe.delete_doc("CRM Intake Form", _FORM, force=True, ignore_permissions=True)
		doc = frappe.get_doc({
			"doctype": "CRM Intake Form",
			"form_name": _FORM,
			"enabled": 1,
			"source": _SOURCE,
			"custom_vertical": _VERTICAL,
			"custom_group": _GROUP,
			"custom_current_program": _PROGRAM,
			"mappings": [
				{"source_field": "phone", "fieldtype": "Phone", "target_table": "lead", "target_field": "mobile_no"},
				{"source_field": "patient_name", "target_table": "lead", "target_field": "first_name"},
				# acq was UNROUTABLE under the old _TABLE dict — the live bug this phase fixes.
				{"source_field": "utm_campaign_field", "target_table": "acq", "target_field": "utm_campaign"},
				{"source_field": "lab_date_field", "fieldtype": "Date", "target_table": "lab", "target_field": "report_date"},
				# weight_kg, not hba1c: the target must be a field THIS grain's contract grants — exactly what the builder's picker now offers.
				{"source_field": "weight_field", "target_table": "lab", "target_field": "weight_kg"},
				{"source_field": "note_field", "target_table": "note", "target_field": "Intake Note"},
			],
		})
		doc.insert(ignore_permissions=True)
		self._made.append(("CRM Intake Form", doc.name))
		frappe.clear_cache(doctype="CRM Intake Form")
		return frappe.get_cached_doc("CRM Intake Form", _FORM)

	def _purge_lead(self, lead_name):
		for dt, flt in (
			("FCRM Note", {"reference_doctype": "CRM Lead", "reference_docname": lead_name}),
			("File", {"attached_to_doctype": "CRM Lead", "attached_to_name": lead_name}),
		):
			for n in frappe.get_all(dt, filters=flt, pluck="name"):
				frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		if frappe.db.exists("CRM Lead", lead_name):
			frappe.delete_doc("CRM Lead", lead_name, force=True, ignore_permissions=True)

	# --- the ONE resolver --------------------------------------------------
	def test_target_doctype_reads_the_section_brain(self):
		self.assertEqual(intake.target_doctype("lead"), "CRM Lead")
		self.assertEqual(intake.target_doctype("acq"), "CRM Acquisition Profile")
		self.assertIsNone(intake.target_doctype("note"))
		self.assertIsNone(intake.target_doctype(""))
		self.assertIsNone(intake.target_doctype("not-a-section"))

	# --- the fold: acq / lab / lead / note all route off the section brain -----
	def test_submission_routes_every_section_via_brain(self):
		builder.sync_form(self.cfg)

		sub = frappe.get_doc({
			"doctype": self.dt,
			"intake_form": self.cfg.name,
			"phone": _PHONE,
			"patient_name": "Asha Routing",
			"utm_campaign_field": "spring_push",
			"lab_date_field": "2026-06-01",
			"weight_field": "62.5",
			"note_field": "Patient asked about diet plan",
		})
		sub.insert(ignore_permissions=True)

		lead_name = frappe.db.get_value("CRM Lead", {
			"mobile_no": to_e164(_PHONE), "custom_vertical": _VERTICAL, "custom_group": _GROUP,
		}, "name")
		self.assertIsNotNone(lead_name, "wildcard router did not create the lead")
		lead = frappe.get_doc("CRM Lead", lead_name)

		# lead (parent section, no child_table_field).
		self.assertEqual(lead.first_name, "Asha Routing")

		# acq (previously unroutable — the C4 capability this phase adds).
		self.assertEqual(len(lead.custom_acquisition_profile), 1)
		self.assertEqual(lead.custom_acquisition_profile[0].utm_campaign, "spring_push")

		# lab (child, multi-row — still lands correctly).
		self.assertEqual(len(lead.custom_lab_profile), 1)
		self.assertAlmostEqual(float(lead.custom_lab_profile[0].weight_kg), 62.5, places=2)
		self.assertEqual(str(lead.custom_lab_profile[0].report_date), "2026-06-01")

		# note (unchanged — still creates an FCRM Note, not a section).
		note_name = frappe.db.get_value(
			"FCRM Note",
			{"reference_doctype": "CRM Lead", "reference_docname": lead.name, "title": "Intake Note"},
			"name",
		)
		self.assertIsNotNone(note_name, "note mapping did not create an FCRM Note")
		self.assertEqual(frappe.db.get_value("FCRM Note", note_name, "content"), "Patient asked about diet plan")
