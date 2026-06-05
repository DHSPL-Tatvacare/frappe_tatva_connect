"""Materialize the partner-API field catalog into `Lead API Field` records.

The catalog (the superset of fields a partner key can be granted) is DATA: the
`Lead API Field` master is the single source of truth, and the partner API reads
its catalog + routing FROM that table. This patch seeds the baseline rows (one per
namespaced `section:fieldname` key) with their routing columns + the `is_row_key`
flag. Idempotent — re-runs add only new keys + backfill routing/is_row_key on old
rows; it never silently drops a key an admin de-listed.

`is_row_key` (A4): a child section is multi-row iff exactly one of its rows carries
is_row_key=1 — that row's fieldname is the upsert-by-key address. A child section
with no row-key is single-row. This replaces the old hardcoded CHILD_CONFIG.

P9 explicitly DE-LISTS `plan:priority_text` and `plan:substage` (the idempotent
upsert never deletes, so we delete them by name here) and adds the new `acq:*`
(single-row) + `drug:*` (multi-row, key `drug:cycle_date`) sections carved out of
CRM Plan Profile.

Adding a new partner field later = add a row here (or in the UI) + the underlying
doctype field. No code change, no rebuild.
"""
import frappe

# (field_key, section_key, target_doctype, child_table_field, label, is_row_key)
# child_table_field is "" for the parent (CRM Lead) section.
_LEAD = "CRM Lead"
_PLAN = "CRM Plan Profile"
_LAB = "CRM Lab Profile"
_ACQ = "CRM Acquisition Profile"
_DRUG = "CRM Drug Program Profile"
_PLAN_CHILD = "custom_plan_profile"
_LAB_CHILD = "custom_lab_profile"
_ACQ_CHILD = "custom_acquisition_profile"
_DRUG_CHILD = "custom_drug_program_profile"

# Catalog keys this patch removes (de-listed in P9). The seed never deletes on its
# own; these are removed explicitly so the split-out doctypes don't leave dead rows.
CATALOG_DELIST = [
	"plan:priority_text",
	"plan:substage",
]

CATALOG_SEED = [
	("lead:mobile_no", "lead", _LEAD, "", "Lead — Mobile No", 0),
	("lead:first_name", "lead", _LEAD, "", "Lead — First Name", 0),
	("lead:last_name", "lead", _LEAD, "", "Lead — Last Name", 0),
	("lead:email", "lead", _LEAD, "", "Lead — Email", 0),
	("lead:custom_dob", "lead", _LEAD, "", "Lead — DOB", 0),
	("lead:custom_gender", "lead", _LEAD, "", "Lead — Gender", 0),
	("lead:custom_alternate_number", "lead", _LEAD, "", "Lead — Alternate Number", 0),
	("lead:custom_city", "lead", _LEAD, "", "Lead — City", 0),
	("lead:custom_preferred_language", "lead", _LEAD, "", "Lead — Preferred Language", 0),
	("lead:custom_patient_id", "lead", _LEAD, "", "Lead — Patient ID", 0),
	("lead:status", "lead", _LEAD, "", "Lead — Status", 0),
	# -- Plan (slimmed): plan + devices + payment + insurance. SINGLE-ROW (no key) —
	# program_start_date is a normal optional field, NOT required; change history is in
	# tabVersion. (Decision 2026-06-05: drop the key to remove write friction.)
	("plan:program_start_date", "plan", _PLAN, _PLAN_CHILD, "Plan — Program Start Date", 0),
	("plan:policy_number", "plan", _PLAN, _PLAN_CHILD, "Plan — Policy Number", 0),
	("plan:member_id", "plan", _PLAN, _PLAN_CHILD, "Plan — Member ID", 0),
	("plan:payment_link", "plan", _PLAN, _PLAN_CHILD, "Plan — Payment Link", 0),
	("plan:plan_name", "plan", _PLAN, _PLAN_CHILD, "Plan — Plan Name", 0),
	# -- Lab. Multi-row, key report_date.
	("lab:report_date", "lab", _LAB, _LAB_CHILD, "Lab — Report Date", 1),
	("lab:hba1c", "lab", _LAB, _LAB_CHILD, "Lab — HbA1c", 0),
	("lab:fbs", "lab", _LAB, _LAB_CHILD, "Lab — FBS", 0),
	("lab:ldl", "lab", _LAB, _LAB_CHILD, "Lab — LDL", 0),
	("lab:hdl", "lab", _LAB, _LAB_CHILD, "Lab — HDL", 0),
	("lab:vldl", "lab", _LAB, _LAB_CHILD, "Lab — VLDL", 0),
	("lab:total_cholesterol", "lab", _LAB, _LAB_CHILD, "Lab — Total Cholesterol", 0),
	("lab:triglycerides", "lab", _LAB, _LAB_CHILD, "Lab — Triglycerides", 0),
	("lab:tsh", "lab", _LAB, _LAB_CHILD, "Lab — TSH", 0),
	("lab:creatinine", "lab", _LAB, _LAB_CHILD, "Lab — Creatinine", 0),
	("lab:egfr", "lab", _LAB, _LAB_CHILD, "Lab — eGFR", 0),
	("lab:alt_sgpt", "lab", _LAB, _LAB_CHILD, "Lab — ALT (SGPT)", 0),
	("lab:ggt", "lab", _LAB, _LAB_CHILD, "Lab — GGT", 0),
	("lab:height_feet", "lab", _LAB, _LAB_CHILD, "Lab — Height (ft)", 0),
	("lab:weight_kg", "lab", _LAB, _LAB_CHILD, "Lab — Weight (kg)", 0),
	("lab:primary_condition", "lab", _LAB, _LAB_CHILD, "Lab — Primary Condition", 0),
	("lab:comorbid_conditions", "lab", _LAB, _LAB_CHILD, "Lab — Co-Morbid Conditions", 0),
	# -- Acquisition (single-row): signup + utm_* + source_* + dx_* funnel.
	("acq:signup_date", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Signup Date", 0),
	("acq:mobile_app_signup_date", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Mobile App Signup Date", 0),
	("acq:signed_up_on_app", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Signed Up on App", 0),
	("acq:platform", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Platform (Mobile App)", 0),
	("acq:user_source", "acq", _ACQ, _ACQ_CHILD, "Acquisition — User Source", 0),
	("acq:utm_source", "acq", _ACQ, _ACQ_CHILD, "Acquisition — UTM Source", 0),
	("acq:utm_medium", "acq", _ACQ, _ACQ_CHILD, "Acquisition — UTM Medium", 0),
	("acq:utm_campaign", "acq", _ACQ, _ACQ_CHILD, "Acquisition — UTM Campaign", 0),
	("acq:utm_term", "acq", _ACQ, _ACQ_CHILD, "Acquisition — UTM Term", 0),
	("acq:utm_content", "acq", _ACQ, _ACQ_CHILD, "Acquisition — UTM Content", 0),
	("acq:utm_disease", "acq", _ACQ, _ACQ_CHILD, "Acquisition — UTM Disease", 0),
	("acq:utm_therapy_area", "acq", _ACQ, _ACQ_CHILD, "Acquisition — UTM Therapy Area", 0),
	("acq:source_campaign", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Source Campaign", 0),
	("acq:source_medium", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Source Medium", 0),
	("acq:source_content", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Source Content", 0),
	("acq:dx_landing_at", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Diagnostic (Dx): Landing Page At", 0),
	("acq:dx_intent_at", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Diagnostic (Dx): Intent At", 0),
	("acq:dx_details_at", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Diagnostic (Dx): Details Submitted At", 0),
	("acq:dx_address_at", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Diagnostic (Dx): Address Submitted At", 0),
	("acq:dx_slot_at", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Diagnostic (Dx): Slot Selected At", 0),
	("acq:dx_confirmed_at", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Diagnostic (Dx): Booking Confirmed At", 0),
	("acq:labtest_booked_at", "acq", _ACQ, _ACQ_CHILD, "Acquisition — Lab Test Booked At", 0),
	# -- Drug Program (multi-row, key cycle_date): oncology PSP + clinical.
	("drug:cycle_date", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Cycle Date", 1),
	("drug:patient_indication", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Patient Indication", 0),
	("drug:cancer_stage", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Cancer Stage", 0),
	("drug:brain_mets_category", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Brain Mets Category", 0),
	("drug:tucavo_brain_mets", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Tukavo Brain Mets", 0),
	("drug:side_effects", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Side Effects", 0),
	("drug:side_effects_detail", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Side Effects Detail", 0),
	("drug:pcr_received", "drug", _DRUG, _DRUG_CHILD, "Drug Program — PCR (Pathology) Report Received", 0),
	("drug:reason_for_no_indication", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Reason for No Indication", 0),
	("drug:tucatinib_chemo_cycle", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Tucatinib Chemo Cycle", 0),
	("drug:tucatinib_indication", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Tucatinib Indication", 0),
	("drug:tucatinib_psp", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Tucatinib PSP", 0),
	("drug:tucatinib_dosage", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Tucatinib Dosage", 0),
	("drug:tukavo_psp", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Tukavo PSP", 0),
	("drug:sigrima_pap_category", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Sigrima PAP Category", 0),
	("drug:sigrima_psp_category", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Sigrima PSP Category", 0),
	("drug:sigrima_psp_enabled", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Sigrima PSP Enabled", 0),
	("drug:ujvira_chemo_cycle_number", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Ujvira Chemo Cycle Number", 0),
	("drug:ujvira_indication", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Ujvira Indication", 0),
	("drug:ujvira_pap_category", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Ujvira PAP Category", 0),
	("drug:ujvira_dosage", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Ujvira Dosage", 0),
	("drug:nivo_chemo_cycle", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Nivolumab Chemo Cycle", 0),
	("drug:nivo_indication", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Nivolumab Indication", 0),
	("drug:nivo_pap_category", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Nivolumab PAP Category", 0),
	("drug:nivo_dosage", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Nivolumab Dosage", 0),
	("drug:capecitabine_dosage", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Capecitabine Dosage", 0),
	("drug:vivitra_enabled", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Vivitra Enabled", 0),
	("drug:vivitra_free_required", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Vivitra Free Required", 0),
	("drug:vivitra_order_cycle_number", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Vivitra Order Cycle Number", 0),
	("drug:vivitra_order_id", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Vivitra Order ID", 0),
	("drug:transition_from_sigrima", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Transition from Sigrima", 0),
	("drug:transition_from_ujvira", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Transition from Ujvira", 0),
	("drug:transition_old_to_new_pap", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Transition Old to New PAP", 0),
	("drug:rx_status", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Prescription (Rx) Status", 0),
	("drug:rx_reviewed_by", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Prescription Reviewed By", 0),
	("drug:rx_rejection_reason", "drug", _DRUG, _DRUG_CHILD, "Drug Program — Prescription Rejection Reason", 0),
]


def execute():
	# De-list first: explicitly remove the keys P9 retired (the upsert never deletes).
	for field_key in CATALOG_DELIST:
		if frappe.db.exists("Lead API Field", field_key):
			frappe.delete_doc("Lead API Field", field_key, ignore_permissions=True, force=True)

	for field_key, section_key, target_doctype, child_table_field, label, is_row_key in CATALOG_SEED:
		_, _, fieldname = field_key.partition(":")
		if frappe.db.exists("Lead API Field", field_key):
			# backfill/repoint the routing columns + the is_row_key flag
			frappe.db.set_value("Lead API Field", field_key, {
				"section_key": section_key,
				"target_doctype": target_doctype,
				"child_table_field": child_table_field,
				"fieldname": fieldname,
				"is_row_key": is_row_key,
			})
			continue
		frappe.get_doc({
			"doctype": "Lead API Field",
			"field_key": field_key,
			"section_key": section_key,
			"target_doctype": target_doctype,
			"child_table_field": child_table_field,
			"fieldname": fieldname,
			"label": label,
			"is_row_key": is_row_key,
		}).insert(ignore_permissions=True)

	frappe.db.commit()

	# ASSERT the catalog resolves the multi-row sections' keys (the CHILD_CONFIG-
	# replacement contract, A4): drug:cycle_date / lab:report_date must each be the row
	# key of their section. Plan is SINGLE-ROW (no key) so it's not asserted here. Fail
	# the migrate loudly rather than silently treat a multi-row child as single-row.
	from tatva_connect.api.partner import _build_catalog
	frappe.cache().delete_value("tatva_connect:lead_api_catalog")
	cat = _build_catalog()
	expected = {"drug": "cycle_date", "lab": "report_date"}
	for section, key_field in expected.items():
		got = cat["section_key_field"].get(section)
		if got != key_field:
			frappe.throw(
				"P9 catalog assert FAILED: section '{0}' row key = {1!r}, expected {2!r}".format(
					section, got, key_field
				)
			)
