"""Retire CRM Lead's dead coordinate pair (custom_latitude / custom_longitude): a duplicate of the clinic anchor that nothing ever read or wrote. The Tatvapractice load writes custom_clinic_latitude/longitude (the anchor Near Me and the automation geofence both read), so these two are surplus — no carry, nothing to preserve. Idempotent: the Custom Field doc AND the physical column go, each guarded."""
import frappe

from tatva_connect.patches import _schema

LEAD = "CRM Lead"
_DEAD = ("custom_latitude", "custom_longitude")


def execute():
	if not frappe.db.table_exists(LEAD):
		return
	for fieldname in _DEAD:
		cf = f"{LEAD}-{fieldname}"
		if frappe.db.exists("Custom Field", cf):
			frappe.delete_doc("Custom Field", cf, ignore_permissions=True, force=True)  # authz-ok: tier-a — migration, runs as Administrator at migrate
		# Deleting the Custom Field leaves the column behind; drop it through the one DDL door.
		if frappe.db.has_column(LEAD, fieldname):
			_schema.ddl(f"ALTER TABLE `tab{LEAD}` DROP COLUMN `{fieldname}`", f"tab{LEAD}")
