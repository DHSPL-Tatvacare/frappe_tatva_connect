# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase A spine — the form-builder scaffolder + the wildcard router, end to end.

Proves the two halves of the spine on a real grain (GoodFlip Care / Anaya / Nivolumab):

  1. `builder.sync_form(cfg)` scaffolds a runtime per-form `custom=1` DocType (and its DB
     table) plus a public Web Form bound to it (anonymous, no login) — idempotently.
  2. A row inserted into that per-form DocType is routed by the WILDCARD after_insert
     (`intake.route_submission`) through the ONE lead-create brain: a CRM Lead is created
     with the form's FORCED grain and the contract's mapped fields.

Teardown removes the lead, the Web Form, the per-form DocType (dropping its table) and the
CRM Intake Form. No fixtures shipped; the grain masters are built minimally in setUp.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import intake
from tatva_connect.automation import seed
from tatva_connect.intake import builder
from tatva_connect.whatsapp.phone import to_e164

_INTAKE_SWITCH = "Lead::Enrolment::intake"
_DEDUP_SWITCH = "Lead::CRM Lead::dedup"

_VERTICAL = "GoodFlip Care"
_GROUP = "Anaya"
_PROGRAM = "Nivolumab"
_SOURCE = "Enrolment Form"
_FORM = "Spine Builder Test"
_PHONE = "9000000007"


class TestIntakeBuilderSpine(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
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
		# Lead created by the router.
		for ld in frappe.get_all("CRM Lead", filters={"mobile_no": to_e164(_PHONE)}, pluck="name"):
			self._purge_lead(ld)
		# Web Form bound to the per-form doctype.
		for wf in frappe.get_all("Web Form", filters={"doc_type": self.dt}, pluck="name"):
			frappe.delete_doc("Web Form", wf, force=True, ignore_permissions=True)
		# The per-form DocType + its table.
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
				{"source_field": "patient_name", "target_table": "lead", "target_field": "first_name"},
				{"source_field": "city_text", "target_table": "lead", "target_field": "custom_city"},
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

	# --- the scaffolder -------------------------------------------------
	def test_sync_form_scaffolds_doctype_and_web_form(self):
		dt, wf = builder.sync_form(self.cfg)

		# Per-form custom DocType exists, with the intrinsic + mapped columns.
		self.assertTrue(frappe.db.exists("DocType", dt))
		self.assertTrue(frappe.db.get_value("DocType", dt, "custom"))
		meta = frappe.get_meta(dt)
		for fn in ("intake_form", "phone", "prescription", "patient_name", "city_text"):
			self.assertTrue(meta.has_field(fn), f"per-form doctype missing column {fn}")
		# The DB table actually exists (the doctype is real, not virtual).
		self.assertTrue(frappe.db.table_exists(dt), f"table tab{dt} was not created")

		# Web Form bound to that doctype, anonymous, no login.
		self.assertTrue(frappe.db.exists("Web Form", wf))
		w = frappe.get_doc("Web Form", wf)
		self.assertEqual(w.doc_type, dt)
		self.assertTrue(w.anonymous)
		self.assertFalse(w.login_required)
		self.assertTrue(any(f.fieldname == "patient_name" for f in w.web_form_fields))

		# Idempotent: a second sync neither errors nor duplicates the Web Form.
		dt2, wf2 = builder.sync_form(self.cfg)
		self.assertEqual((dt2, wf2), (dt, wf))
		self.assertEqual(frappe.db.count("Web Form", {"doc_type": dt}), 1)

	# --- re-sync is additive, never destructive --------------------------
	def test_resync_adds_missing_field_without_dropping(self):
		builder.sync_form(self.cfg)
		self.cfg.append("mappings", {"source_field": "extra_note_field", "target_table": "lead", "target_field": "last_name"})
		builder.sync_form(self.cfg)
		meta = frappe.get_meta(self.dt)
		self.assertTrue(meta.has_field("extra_note_field"), "re-sync did not add the new column")
		self.assertTrue(meta.has_field("patient_name"), "re-sync dropped an existing column")

	# --- the wildcard router (ONE brain) --------------------------------
	def test_submission_routes_to_lead_via_brain(self):
		builder.sync_form(self.cfg)

		# Insert a row into the per-form sink, firing after_insert normally -> the wildcard
		# router (doc_events["*"]) picks it up and folds it to a lead via partner._upsert_one.
		sub = frappe.get_doc({
			"doctype": self.dt,
			"intake_form": self.cfg.name,  # the Web Form sets this from the field default server-side
			"phone": _PHONE,
			"patient_name": "Asha Spine",
			"city_text": "Bengaluru",
		})
		sub.insert(ignore_permissions=True)

		lead_name = frappe.db.get_value("CRM Lead", {
			"mobile_no": to_e164(_PHONE), "custom_vertical": _VERTICAL, "custom_group": _GROUP,
		}, "name")
		self.assertIsNotNone(lead_name, "wildcard router did not create the lead")
		lead = frappe.get_doc("CRM Lead", lead_name)
		# Forced grain (submitter could not influence any of these).
		self.assertEqual(lead.custom_vertical, _VERTICAL)
		self.assertEqual(lead.custom_group, _GROUP)
		self.assertEqual(lead.custom_current_program, _PROGRAM)
		self.assertEqual(lead.source, _SOURCE)
		# Mapped fields.
		self.assertEqual(lead.first_name, "Asha Spine")
		self.assertEqual(lead.custom_city, "Bengaluru")

	# --- cheap guard: a non-intake doctype is ignored -------------------
	def test_router_ignores_non_intake_doctype(self):
		builder.sync_form(self.cfg)
		# A ToDo insert must NOT be folded into a lead (no phone, not in the guard set).
		# This just proves route_submission early-returns without error on a foreign doctype.
		self.assertNotIn("ToDo", intake._intake_doctypes())
		td = frappe.get_doc({"doctype": "ToDo", "description": "spine guard probe"})
		td.insert(ignore_permissions=True)
		self._made.append(("ToDo", td.name))
