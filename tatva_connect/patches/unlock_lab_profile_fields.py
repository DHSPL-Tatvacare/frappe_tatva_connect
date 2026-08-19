"""Lab results become editable — the rep captures what the patient tells them on the call.

Fifteen `CRM Lab Profile` columns shipped `read_only` in the 2026-06-05 schema-as-code commit, on the
reading that a clinical result belongs to a lab report and not to a sales call: alt_sgpt, creatinine,
egfr, fbs, ggt, hba1c, hdl, height_feet, ldl, report_date, total_cholesterol, triglycerides, tsh, vldl,
weight_kg. Owner decision 2026-08-19 reverses it — a rep asking a patient their height or their last
HbA1c is ordinary work, and the lock only surfaced as `Field lab:height_feet is not editable here` at
the moment of Save, after the rep had already typed the answer.

Removing them from `fixtures/property_setter.json` only stops them SHIPPING; frappe never deletes a
Property Setter a site already holds, so the row is deleted here or every existing site stays locked.

`report_date` STAYS read-only in practice and needs no exception: it is the lab section's `row_key_field`,
and `lead.detail._is_readonly` refuses a row key before it ever looks at a docfield — a row key is the
row's address, not a value on it. One rule, one place; this patch does not restate it.

Idempotent (delete_doc guarded by exists). No schema_setup twin: a fresh site's fixture no longer declares
these setters, so there is nothing for it to remove.
"""

import frappe

_DOCTYPE = "CRM Lab Profile"


def execute():
	for name in frappe.get_all(
		"Property Setter",
		filters={"doc_type": _DOCTYPE, "property": "read_only"},
		pluck="name",
	):
		frappe.delete_doc("Property Setter", name, force=True, ignore_permissions=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
	frappe.clear_cache(doctype=_DOCTYPE)
	frappe.db.commit()
