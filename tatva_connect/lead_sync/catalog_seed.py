"""Catalog rows for the fields the Facebook fold stamps; without one a key has no declared home."""
import frappe

# field_key -> (label, fieldname). All parent (CRM Lead) rows; no child_table_field.
_ROWS = {
	"lead:facebook_lead_id": ("Facebook Lead ID", "facebook_lead_id"),
	"lead:facebook_form_id": ("Facebook Form ID", "facebook_form_id"),
	"lead:custom_source_origin": ("Source Origin", "custom_source_origin"),
}


def ensure_rows():
	"""Idempotent: the catalog is operator data, so only the rows our own code depends on are asserted."""
	for field_key, (label, fieldname) in _ROWS.items():
		if frappe.db.exists("CRM Lead API Field", field_key):
			continue
		frappe.get_doc(
			{
				"doctype": "CRM Lead API Field",
				"field_key": field_key,
				"label": label,
				"section": "lead",
				"fieldname": fieldname,
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
	frappe.db.commit()
