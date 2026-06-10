"""Seed the Nivolumab (Tishtha) intake config + a few sample masters.

Idempotent. The CRM Intake Form row is what makes the generic processor route a
Nivolumab submission; the sample doctors/hospitals just give the pick-lists some
content to demo (real ones grow as users type new names).
"""
import frappe

DOCTORS = [("Dr. Karthik S Udupa", "Manipal"), ("Dr. A Rao", "Pune"), ("Dr. S Mehta", "Mumbai")]
HOSPITALS = [("Kasturba Manipal", "Manipal"), ("Apollo", "Pune"), ("Tata Memorial", "Mumbai")]

MAPPINGS = [
	{"source_field": "patient_name", "target": "lead:first_name"},
	{"source_field": "city", "target": "lead:custom_city"},
	{"source_field": "state", "manual_field": "state_manual", "target": "lead:custom_state"},
	{"source_field": "doctor", "manual_field": "doctor_manual", "master_doctype": "CRM Doctor", "target": "care:doctor_name"},
	{"source_field": "hospital", "manual_field": "hospital_manual", "master_doctype": "CRM Hospital", "target": "care:hospital_name"},
	{"source_field": "nivolumab_dosage", "manual_field": "nivolumab_dosage_manual", "target": "plan:nivo_dosage"},
	{"source_field": "nivolumab_indication", "target": "plan:nivo_indication"},
	{"source_field": "remarks", "target": "note:Enrolment remarks"},
]


def execute():
	# The Intake Form Links source -> CRM Lead Source "Nivolumab", a custom source not
	# created by FCRM. Ensure it exists so a fresh-DB seed doesn't fail Link validation.
	if not frappe.db.exists("CRM Lead Source", "Nivolumab"):
		frappe.get_doc({"doctype": "CRM Lead Source", "source_name": "Nivolumab"}).insert(ignore_permissions=True)
	for dn, city in DOCTORS:
		if not frappe.db.exists("CRM Doctor", dn):
			frappe.get_doc({"doctype": "CRM Doctor", "doctor_name": dn, "city": city}).insert(ignore_permissions=True)
	for hn, city in HOSPITALS:
		if not frappe.db.exists("CRM Hospital", hn):
			frappe.get_doc({"doctype": "CRM Hospital", "hospital_name": hn, "city": city}).insert(ignore_permissions=True)

	if not frappe.db.exists("CRM Intake Form", "Nivolumab Enrolment"):
		frappe.get_doc(
			{
				"doctype": "CRM Intake Form",
				"form_name": "Nivolumab Enrolment",
				"enabled": 1,
				"description": "Tishtha Nivolumab patient enrolment (replaces the LSQ form).",
				"source": "Nivolumab",
				"custom_vertical": "GoodFlip Care",
				"custom_group": "Anaya",
				"custom_current_program": "Nivolumab",
				"custom_origin_vertical": "GoodFlip Care",
				"mappings": MAPPINGS,
			}
		).insert(ignore_permissions=True)
