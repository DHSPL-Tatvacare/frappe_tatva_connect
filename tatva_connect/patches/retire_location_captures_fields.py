"""Retire the superseded CRM Lead "Location Captures" section + HTML field.

The activity/location rebuild collapsed the two separate Desk sections (Activity Timeline +
Location Captures) into ONE unified "Activity & Location" view rendered into
custom_activity_timeline_html. The old fields are gone from the fixture, but Frappe's fixture
sync does NOT delete a Custom Field that disappears from the JSON — so the stale rows + DB
columns linger. This removes them. Idempotent: a no-op once they're gone.
"""
import frappe

_STALE = (
	"CRM Lead-custom_location_captures_html",
	"CRM Lead-custom_location_captures_section",
)


def execute():
	for name in _STALE:
		if frappe.db.exists("Custom Field", name):
			frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
