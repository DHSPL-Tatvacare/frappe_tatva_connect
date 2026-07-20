# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Identity fields (first_name, last_name, mobile_no, custom_gender, custom_dob) were marked read_only as "field governance" — but a person must TYPE them on the create form to bring a lead into existence (the lead does not exist yet). read_only enforces nothing server-side (it is a form hint), so it protected no data; its only effect was that Frappe hides an empty read_only field, erasing the fields the create form needs. Removing the setters makes them writable at creation again. Post-creation, role-based read-only for these fields is a separate server-side lifecycle rule (validate: not-new + interactive user + role -> block), NOT a field flag. custom_patient_id KEEPS its setter: it is API-minted from the source system, never typed on create. A fresh site never carries these (out of the fixture now); this heals a site that already ran them."""
import frappe

STALE = (
	"CRM Lead-mobile_no-read_only",
	"CRM Lead-first_name-read_only",
	"CRM Lead-last_name-read_only",
	"CRM Lead-custom_gender-read_only",
	"CRM Lead-custom_dob-read_only",
)


def execute():
	for name in STALE:
		if frappe.db.exists("Property Setter", name):
			frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	frappe.clear_cache()
