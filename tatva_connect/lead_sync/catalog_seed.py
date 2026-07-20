"""Catalog rows for the fields the Facebook fold stamps; without one a key has no declared home."""
import frappe

# field_key -> (label, fieldname, section).
_ROWS = {
	"lead:facebook_lead_id": ("Facebook Lead ID", "facebook_lead_id", "lead"),
	"lead:facebook_form_id": ("Facebook Form ID", "facebook_form_id", "lead"),
	"lead:custom_source_origin": ("Source Origin", "custom_source_origin", "lead"),
	# The acquisition touch: which campaign reached this patient and when. utm_* is platform-neutral.
	"acq:touch_at": ("Touched At", "touch_at", "acq"),
	"acq:utm_source": ("UTM Source", "utm_source", "acq"),
	"acq:utm_campaign": ("UTM Campaign", "utm_campaign", "acq"),
}


def ensure_rows():
	"""Idempotent: the catalog is operator data, so only the rows our own code depends on are asserted."""
	for field_key, (label, fieldname, section) in _ROWS.items():
		if frappe.db.exists("CRM Lead API Field", field_key):
			continue
		frappe.get_doc(
			{
				"doctype": "CRM Lead API Field",
				"field_key": field_key,
				"label": label,
				"section": section,
				"fieldname": fieldname,
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
	frappe.db.commit()
