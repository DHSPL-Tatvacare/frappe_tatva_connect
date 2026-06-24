"""Delete the superseded CRM Lead "Location Captures" section + HTML Custom Fields (folded into the unified Activity & Location view); fixture sync never drops them, so remove explicitly; idempotent."""
import frappe

_STALE = (
	"CRM Lead-custom_location_captures_html",
	"CRM Lead-custom_location_captures_section",
)


def execute():
	for name in _STALE:
		if frappe.db.exists("Custom Field", name):
			frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
