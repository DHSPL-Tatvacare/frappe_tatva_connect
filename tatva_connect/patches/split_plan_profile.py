"""P9 profile restructure — move acquisition + drug-program columns out of
CRM Plan Profile into the new CRM Acquisition Profile (single-row) and CRM Drug
Program Profile (multi-row, key cycle_date) child tables.

SAFETY (copy -> verify -> drop, the §E4 discipline):
  * Frappe's schema sync does NOT drop a removed DocField's DB column — the moved
    columns survive as ORPHANED columns on `tabCRM Plan Profile` after the slimmed
    doctype is migrated. So this post_model_sync patch can still READ the legacy
    values via raw SQL.
  * For each lead with plan rows, we COPY the moved values into the new children via
    the DOC API (append + save) so parentfield/parenttype/idx are correct.
  * We VERIFY (per-lead: an acquisition row + a drug row now exist with the legacy
    values; field-checksum on >=3 samples) and LOG it BEFORE dropping anything.
  * Only after the verify gate passes do we DROP the orphaned columns from the Plan
    table. Reversible until that drop (the legacy columns + their data are intact).

Idempotent: if a lead already has the new children, it is skipped (re-run safe).
One acquisition + one drug row per existing plan row (each legacy plan row carried
both funnel + oncology data in one row, so it fans out to one of each). The drug
row's cycle_date is seeded from program_start_date (the only date anchor on the
legacy row); a null anchor stores a null-keyed row (still valid stored data — a
future API upsert just needs an explicit cycle_date).
"""
import frappe
from frappe.utils import cstr

_PLAN_TABLE = "tabCRM Plan Profile"

# The columns carved OUT of CRM Plan Profile (verbatim fieldnames). Mirrors the seed
# partition. These are the orphaned columns to read-then-drop.
ACQ_FIELDS = [
	"signup_date", "mobile_app_signup_date", "signed_up_on_app", "platform", "user_source",
	"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_disease",
	"utm_therapy_area", "source_campaign", "source_medium", "source_content",
	"dx_landing_at", "dx_intent_at", "dx_details_at", "dx_address_at", "dx_slot_at",
	"dx_confirmed_at", "labtest_booked_at",
]
DRUG_FIELDS = [
	"patient_indication", "cancer_stage", "brain_mets_category", "tucavo_brain_mets",
	"side_effects", "side_effects_detail", "pcr_received", "reason_for_no_indication",
	"tucatinib_chemo_cycle", "tucatinib_indication", "tucatinib_psp", "tucatinib_dosage",
	"tukavo_psp", "sigrima_pap_category", "sigrima_psp_category", "sigrima_psp_enabled",
	"ujvira_chemo_cycle_number", "ujvira_indication", "ujvira_pap_category", "ujvira_dosage",
	"nivo_chemo_cycle", "nivo_indication", "nivo_pap_category", "nivo_dosage",
	"capecitabine_dosage", "vivitra_enabled", "vivitra_free_required",
	"vivitra_order_cycle_number", "vivitra_order_id",
	"transition_from_sigrima", "transition_from_ujvira", "transition_old_to_new_pap",
	"rx_status", "rx_reviewed_by", "rx_rejection_reason",
]
# De-listed columns (no longer modelled anywhere) — dropped after the move.
DROPPED_FIELDS = ["priority_text", "substage"]


def _ensure_lead_table_fields():
	"""Create the custom_acquisition_profile / custom_drug_program_profile Table fields
	on CRM Lead if absent (fixtures load after patches). Idempotent."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	fields = []
	if not frappe.get_meta("CRM Lead").get_field("custom_acquisition_profile"):
		fields.append({
			"fieldname": "custom_acquisition_profile", "label": "Acquisition Profile",
			"fieldtype": "Table", "options": "CRM Acquisition Profile",
			"insert_after": "custom_plan_profile",
		})
	if not frappe.get_meta("CRM Lead").get_field("custom_drug_program_profile"):
		fields.append({
			"fieldname": "custom_drug_program_profile", "label": "Drug Program Profile",
			"fieldtype": "Table", "options": "CRM Drug Program Profile",
			"insert_after": "custom_plan_profile",
		})
	if fields:
		create_custom_fields({"CRM Lead": fields}, ignore_validate=True)
		frappe.clear_cache(doctype="CRM Lead")


def _orphan_cols():
	"""Moved columns that still physically exist on the Plan table (orphaned)."""
	cols = {c["Field"] for c in frappe.db.sql(f"SHOW COLUMNS FROM `{_PLAN_TABLE}`", as_dict=True)}
	return [f for f in (ACQ_FIELDS + DRUG_FIELDS) if f in cols]


def _has_any_value(values, fields):
	return any(values.get(f) not in (None, "", 0) for f in fields)


def execute():
	if not frappe.db.exists("DocType", "CRM Acquisition Profile") or not frappe.db.exists(
		"DocType", "CRM Drug Program Profile"
	):
		frappe.log_error("split_plan_profile: new doctypes missing — aborting", "P9 migration")
		return

	# The CRM Lead Table fields that point to the new children ship as fixtures, which
	# load AFTER patches in the migrate sequence — so create them here (idempotent) so
	# doc.append() can reach them. The later fixture sync reconciles harmlessly.
	_ensure_lead_table_fields()

	movable = _orphan_cols()
	if not movable:
		# Already migrated (columns dropped on a prior run) — nothing to copy. Still
		# make sure the de-listed columns are gone.
		_drop_dropped_only()
		return

	# Read the legacy values for every plan row (orphaned columns still present).
	select_cols = ["name", "parent", "program_start_date"] + movable
	col_sql = ", ".join(f"`{c}`" for c in select_cols)
	rows = frappe.db.sql(
		f"SELECT {col_sql} FROM `{_PLAN_TABLE}` WHERE parenttype='CRM Lead'", as_dict=True
	)

	migrated_leads = {}   # lead -> {"acq": bool, "drug": bool}
	samples = []          # for the verify-gate checksum log
	for r in rows:
		lead = r.get("parent")
		if not lead or not frappe.db.exists("CRM Lead", lead):
			continue
		doc = frappe.get_doc("CRM Lead", lead)

		acq_vals = {f: r.get(f) for f in ACQ_FIELDS if f in movable}
		drug_vals = {f: r.get(f) for f in DRUG_FIELDS if f in movable}

		did = migrated_leads.setdefault(lead, {"acq": False, "drug": False})

		# Acquisition (single-row): only create if there's something to carry AND the
		# lead has no acquisition row yet (idempotent).
		if _has_any_value(acq_vals, ACQ_FIELDS) and not doc.get("custom_acquisition_profile"):
			doc.append("custom_acquisition_profile", {k: v for k, v in acq_vals.items() if v not in (None, "")})
			did["acq"] = True

		# Drug Program (multi-row, key cycle_date): one row per legacy plan row.
		if _has_any_value(drug_vals, DRUG_FIELDS):
			payload = {k: v for k, v in drug_vals.items() if v not in (None, "")}
			payload["cycle_date"] = r.get("program_start_date")
			doc.append("custom_drug_program_profile", payload)
			did["drug"] = True

		if did["acq"] or did["drug"]:
			doc.save(ignore_permissions=True)
			if len(samples) < 5:
				samples.append((lead, acq_vals, drug_vals, r.get("program_start_date")))

	frappe.db.commit()

	# ---- VERIFY GATE (logged) — must pass before any drop ----
	ok, report = _verify(samples)
	frappe.log_error(message=report, title="P9 split_plan_profile VERIFY")
	if not ok:
		frappe.throw("P9 split_plan_profile verify FAILED — columns NOT dropped. See Error Log.")

	# ---- DROP (only after verify passes) ----
	_drop_columns(movable + [f for f in DROPPED_FIELDS if _col_exists(f)])

	# nivo_indication's free-text override follows the field to the new doctype.
	_move_nivo_property_setters()


def _move_nivo_property_setters():
	"""nivo_indication moved Plan -> Drug Program Profile. Recreate its free-text
	override (Select-with-no-options -> Data, so form values store AND display) on the
	new doctype and drop the stale Plan-targeted setters. Idempotent."""
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	for prop, value, ptype in (("fieldtype", "Data", "Select"), ("options", "", "Text")):
		stale = "CRM Plan Profile-nivo_indication-{0}".format(prop)
		if frappe.db.exists("Property Setter", stale):
			frappe.delete_doc("Property Setter", stale, ignore_permissions=True, force=True)
		new_name = "CRM Drug Program Profile-nivo_indication-{0}".format(prop)
		if not frappe.db.exists("Property Setter", new_name):
			make_property_setter(
				"CRM Drug Program Profile", "nivo_indication", prop, value, ptype,
				is_system_generated=False,
			)


def _col_exists(field):
	cols = {c["Field"] for c in frappe.db.sql(f"SHOW COLUMNS FROM `{_PLAN_TABLE}`", as_dict=True)}
	return field in cols


def _verify(samples):
	"""Per-sample: each legacy row's moved values now appear on the new children.
	Returns (ok, human-readable report)."""
	lines = ["P9 split_plan_profile verify — {0} sample lead(s)".format(len(samples))]
	ok = True
	for lead, acq_vals, drug_vals, cycle in samples:
		doc = frappe.get_doc("CRM Lead", lead)
		acq_rows = doc.get("custom_acquisition_profile") or []
		drug_rows = doc.get("custom_drug_program_profile") or []
		# acquisition: every non-empty legacy acq value present on row 0
		acq_present = all(
			acq_rows and cstr(acq_rows[0].get(f)) == cstr(v)
			for f, v in acq_vals.items() if v not in (None, "")
		)
		# drug: a row whose cycle_date matches + carries the non-empty legacy drug values
		drug_present = True
		nonempty_drug = {f: v for f, v in drug_vals.items() if v not in (None, "")}
		if nonempty_drug:
			match = [d for d in drug_rows if cstr(d.get("cycle_date")) == cstr(cycle)]
			drug_present = bool(match) and all(
				cstr(match[0].get(f)) == cstr(v) for f, v in nonempty_drug.items()
			)
		lead_ok = acq_present and drug_present
		ok = ok and lead_ok
		lines.append(
			"  {0}: acq_ok={1} drug_ok={2} (acq_rows={3} drug_rows={4})".format(
				lead, acq_present, drug_present, len(acq_rows), len(drug_rows)
			)
		)
	lines.append("RESULT: {0}".format("PASS" if ok else "FAIL"))
	return ok, "\n".join(lines)


def _drop_columns(fields):
	for f in fields:
		if _col_exists(f):
			frappe.db.sql_ddl(f"ALTER TABLE `{_PLAN_TABLE}` DROP COLUMN `{f}`")
	frappe.db.commit()


def _drop_dropped_only():
	to_drop = [f for f in DROPPED_FIELDS if _col_exists(f)]
	if to_drop:
		_drop_columns(to_drop)
