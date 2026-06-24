# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 0 spike (Invariant A.13) — prove intake folded into the partner brain is byte-parity.

`intake.process_submission` no longer runs its own new_doc/find/route/insert: it resolves the
form into a partner-shaped payload + grain descriptor and hands it to `partner._upsert_one`
(the ONE lead-create brain). This test proves, for a representative Niva-style intake:

  * CREATE through the NEW path -> a routed, deduped CRM Lead with the right mobile (+E.164),
    forced grain, mapped lead fields, the child profile row(s), the FCRM Note(s) and the
    prescription attachment.
  * a SECOND submit (same phone+grain) UPDATES that lead, never a duplicate.
  * NEW-path output == `_process_submission_legacy` output, field-by-field, on independent leads.

Grain: GoodFlip Care / Anaya / Nivolumab (forced-program form). The fixtures build the minimal
grain masters; teardown removes everything created.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect import intake
from tatva_connect.automation import seed
from tatva_connect.whatsapp.phone import to_e164

_INTAKE_SWITCH = "Lead::Enrolment::intake"
_DEDUP_SWITCH = "Lead::CRM Lead::dedup"

# The grain under test (mirrors the live "Nivolumab Enrolment" form).
_VERTICAL = "GoodFlip Care"
_GROUP = "Anaya"
_PROGRAM = "Nivolumab"
_SOURCE = "Enrolment Form"
_FORM = "Nivolumab Enrolment (test)"


class TestIntakeBrainParity(FrappeTestCase):
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

	def tearDown(self):
		frappe.set_user("Administrator")
		# Leads created by either path (matched by the test phones) + their notes/files.
		for phone in (self._e164("9000000001"), self._e164("9000000002"), self._e164("9000000003")):
			for ld in frappe.get_all("CRM Lead", filters={"mobile_no": phone}, pluck="name"):
				self._purge_lead(ld)
		for dt, name in reversed(self._made):
			if frappe.db.exists(dt, name):
				frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
		self._set_switch(_INTAKE_SWITCH, 0)
		self._set_switch(_DEDUP_SWITCH, 0)

	# --- helpers ---------------------------------------------------------
	def _set_switch(self, key, on):
		if frappe.db.exists("CRM Tatva Automation", key):
			frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1 if on else 0)

	def _e164(self, ten_digits):
		return to_e164(ten_digits)

	def _ensure(self, doctype, name, values):
		"""Create a master if absent; track only what we created so teardown is clean."""
		if frappe.db.exists(doctype, name):
			return name
		d = frappe.new_doc(doctype)
		d.update(values)
		# Most of these masters autoname on their *_name field -> name == the value.
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
				{"source_field": "nivolumab_dosage", "manual_field": "nivolumab_dosage_manual",
				 "target_table": "drug", "target_field": "nivo_dosage"},
				{"source_field": "nivolumab_indication", "target_table": "drug", "target_field": "nivo_indication"},
				{"source_field": "remarks", "target_table": "note", "target_field": "Enrolment Remarks"},
			],
		})
		doc.insert(ignore_permissions=True)
		self._made.append(("CRM Intake Form", doc.name))
		# get_cached_doc is what process_submission reads — clear so it sees this fresh row.
		frappe.clear_cache(doctype="CRM Intake Form")
		return frappe.get_cached_doc("CRM Intake Form", _FORM)

	def _submission(self, phone, *, name="Asha Test", dosage="40 mg",
	                indication="NSCLC", remarks="Started cycle 1", prescription=None):
		city = self._ensure("CRM City", "Bengaluru::Karnataka", {"city_name": "Bengaluru", "state": "Karnataka"})
		doc = frappe.get_doc({
			"doctype": "CRM Enrolment Submission",
			"intake_form": _FORM,
			"patient_name": name,
			# Phone fieldtype requires a country code on insert (what the real Web Form
			# control always sends); lookups below normalise via to_e164 either way.
			"phone": phone if phone.startswith("+") else "+91 " + phone,
			"state": "Karnataka",
			"city": city,
			"nivolumab_dosage": dosage,
			"nivolumab_indication": indication,
			"remarks": remarks,
			"prescription": prescription,
		})
		# Insert WITHOUT firing after_insert — we drive the processor explicitly per path
		# so the test controls which one runs against a virgin row.
		doc.flags.ignore_after_insert_event = True
		doc.insert(ignore_permissions=True)
		self._made.append(("CRM Enrolment Submission", doc.name))
		return doc

	def _make_private_file(self, attached_to):
		"""A stored private File standing in for an uploaded prescription; returns its file_url."""
		f = frappe.get_doc({
			"doctype": "File",
			"file_name": "rx.pdf",
			"content": b"%PDF-1.4 test prescription",
			"attached_to_doctype": "CRM Enrolment Submission",
			"attached_to_name": attached_to,
			"is_private": 1,
		}).insert(ignore_permissions=True)
		self._made.append(("File", f.name))
		return f.file_url

	def _purge_lead(self, lead_name):
		for dt, flt in (
			("FCRM Note", {"reference_doctype": "CRM Lead", "reference_docname": lead_name}),
			("File", {"attached_to_doctype": "CRM Lead", "attached_to_name": lead_name}),
		):
			for n in frappe.get_all(dt, filters=flt, pluck="name"):
				frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		if frappe.db.exists("CRM Lead", lead_name):
			frappe.delete_doc("CRM Lead", lead_name, force=True, ignore_permissions=True)

	def _lead_for(self, phone):
		name = frappe.db.get_value("CRM Lead", {
			"mobile_no": self._e164(phone),
			"custom_vertical": _VERTICAL, "custom_group": _GROUP,
		}, "name")
		return frappe.get_doc("CRM Lead", name) if name else None

	# --- CREATE through the NEW path ------------------------------------
	def test_create_routes_and_maps(self):
		phone = "9000000001"
		sub = self._submission(phone)
		rx_url = self._make_private_file(sub.name)
		sub.db_set("prescription", rx_url, update_modified=False)
		sub.reload()

		intake.process_submission(sub)

		lead = self._lead_for(phone)
		self.assertIsNotNone(lead, "new path did not create the lead")
		# mobile +E.164 + forced grain (submitter could not influence any of these)
		self.assertEqual(lead.mobile_no, self._e164(phone))
		self.assertEqual(lead.custom_vertical, _VERTICAL)
		self.assertEqual(lead.custom_group, _GROUP)
		self.assertEqual(lead.custom_current_program, _PROGRAM)
		self.assertEqual(lead.source, _SOURCE)
		# mapped lead field
		self.assertEqual(lead.first_name, "Asha Test")
		# child profile row (dosage/indication land on the drug-program child)
		drug = lead.get("custom_drug_program_profile")
		self.assertTrue(drug, "drug program profile row missing")
		self.assertEqual(drug[0].nivo_dosage, "40 mg")
		self.assertEqual(drug[0].nivo_indication, "NSCLC")
		# FCRM Note
		notes = frappe.get_all("FCRM Note", filters={
			"reference_doctype": "CRM Lead", "reference_docname": lead.name,
		}, fields=["title", "content"])
		self.assertTrue(any(n.title == "Enrolment Remarks" and n.content == "Started cycle 1"
		                    for n in notes), f"note not created: {notes}")
		# prescription surfaced on the lead, private
		files = frappe.get_all("File", filters={
			"attached_to_doctype": "CRM Lead", "attached_to_name": lead.name,
		}, fields=["is_private"])
		self.assertTrue(files, "prescription not attached to lead")
		self.assertTrue(all(f.is_private for f in files), "prescription not private")
		# staging row stamped
		sub.reload()
		self.assertEqual(sub.lead, lead.name)
		self.assertTrue(sub.processed)

	# --- SECOND submit dedups (UPDATE, not duplicate) -------------------
	def test_second_submit_updates_same_lead(self):
		phone = "9000000001"
		first = self._submission(phone, name="Asha Test", indication="NSCLC")
		intake.process_submission(first)
		lead1 = self._lead_for(phone)

		second = self._submission(phone, name="Asha Updated", indication="RCC")
		intake.process_submission(second)
		lead2 = self._lead_for(phone)

		self.assertEqual(lead1.name, lead2.name, "second submit created a duplicate lead")
		self.assertEqual(
			frappe.db.count("CRM Lead", {
				"mobile_no": self._e164(phone), "custom_vertical": _VERTICAL, "custom_group": _GROUP,
			}), 1, "more than one lead on the grain")
		# the update took
		self.assertEqual(lead2.first_name, "Asha Updated")
		self.assertEqual(lead2.get("custom_drug_program_profile")[0].nivo_indication, "RCC")

	# --- PARITY: NEW path == legacy, on independent leads ---------------
	def test_parity_new_vs_legacy(self):
		new_sub = self._submission("9000000002", name="Bina New")
		intake.process_submission(new_sub)
		new_lead = self._lead_for("9000000002")

		legacy_sub = self._submission("9000000003", name="Bina New")
		intake._process_submission_legacy(legacy_sub)
		legacy_lead = self._lead_for("9000000003")

		self.assertIsNotNone(new_lead)
		self.assertIsNotNone(legacy_lead)

		# Compare every field that intake controls (skip identity/audit + the phone we
		# deliberately differ on, and the source_origin which embeds the same form name).
		skip = {
			"name", "creation", "modified", "modified_by", "owner", "idx",
			"mobile_no", "phone", "lead_name", "naming_series",
		}
		new_d, legacy_d = new_lead.as_dict(), legacy_lead.as_dict()
		parent_keys = set(new_d) | set(legacy_d)
		mismatches = {}
		for k in parent_keys - skip:
			nv, lv = new_d.get(k), legacy_d.get(k)
			if isinstance(nv, list) or isinstance(lv, list):
				continue  # child tables compared separately below
			if nv != lv:
				mismatches[k] = (nv, lv)
		self.assertEqual(mismatches, {}, f"parent-field parity drift: {mismatches}")

		# child drug-program profile: same mapped values (dosage/indication land here, not plan)
		new_drug = new_lead.get("custom_drug_program_profile")
		legacy_drug = legacy_lead.get("custom_drug_program_profile")
		self.assertEqual(len(new_drug), len(legacy_drug))
		self.assertEqual(new_drug[0].nivo_dosage, legacy_drug[0].nivo_dosage)
		self.assertEqual(new_drug[0].nivo_indication, legacy_drug[0].nivo_indication)

		# notes: same set of (title, content)
		def note_set(lead):
			return {(n.title, n.content) for n in frappe.get_all(
				"FCRM Note",
				filters={"reference_doctype": "CRM Lead", "reference_docname": lead.name},
				fields=["title", "content"])}
		self.assertEqual(note_set(new_lead), note_set(legacy_lead))
