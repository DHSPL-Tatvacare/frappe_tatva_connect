"""Catalog rows for the fields the Facebook fold stamps; without one a key has no declared home."""
import frappe

# field_key -> (label, fieldname, section, filterable, sortable). The flags are the CREATION baseline only.
_ROWS = {
	"lead:facebook_lead_id": ("Facebook Lead ID", "facebook_lead_id", "lead", 1, 1),
	"lead:facebook_form_id": ("Facebook Form ID", "facebook_form_id", "lead", 1, 1),
	"lead:custom_source_origin": ("Source Origin", "custom_source_origin", "lead", 1, 1),
	# The acquisition touch: which campaign reached this patient and when. utm_* is platform-neutral.
	"acq:touch_at": ("Touched At", "touch_at", "acq", 0, 0),
	"acq:utm_source": ("UTM Source", "utm_source", "acq", 1, 1),
	"acq:utm_campaign": ("UTM Campaign", "utm_campaign", "acq", 1, 1),
}


def ensure_rows():
	"""Idempotent: the catalog is operator data, so only the rows our own code depends on are asserted.

	The existence gate below means the baseline flags are written ONCE, at creation, and never re-imposed —
	an operator who later clears filterable/sortable keeps that choice on every subsequent migrate."""
	for field_key, (label, fieldname, section, filterable, sortable) in _ROWS.items():
		if frappe.db.exists("CRM Lead API Field", field_key):
			continue
		frappe.get_doc(
			{
				"doctype": "CRM Lead API Field",
				"field_key": field_key,
				"label": label,
				"section": section,
				"fieldname": fieldname,
				"filterable": filterable,
				"sortable": sortable,
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-c — after_migrate, no session user
	frappe.db.commit()
