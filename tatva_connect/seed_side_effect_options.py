"""Seed the CRM Side Effect Option vocabulary, run on after_migrate.

Allowed under invariant #5: this is a fixed CLINICAL vocabulary (the allowed values of
the CRM Drug Program Profile.side_effects_detail Table MultiSelect), identical in every
deployment — not operator config. Idempotent: inserts only the options that don't yet
exist (the option name is the natural key).
"""
import frappe

OPTIONS = (
	"No side effects",
	"Diarrhea",
	"Fatigue",
	"Nausea and Vomiting",
	"Rash or Skin Changes",
	"Nail Changes & Hand-Foot Syndrome",
)


def execute():
	for option_name in OPTIONS:
		if frappe.db.exists("CRM Side Effect Option", option_name):
			continue
		frappe.get_doc(
			{"doctype": "CRM Side Effect Option", "option_name": option_name}
		).insert(ignore_permissions=True)
	frappe.db.commit()
