"""Materialize the partner-API field catalog into `Lead API Field` records.

The catalog (the superset of fields a partner key can be granted) lives in code:
`tatva_connect.api.partner.CATALOG`. This patch turns each key into a `Lead API
Field` row so the per-partner `Allowed Fields` grid is a clean dropdown. Idempotent
— re-runs add only new keys; it does not delete (an admin may have de-listed one).
"""
import frappe

from tatva_connect.api.partner import CATALOG, catalog_label


def execute():
	for key in CATALOG:
		section, _, fieldname = key.partition(":")
		if frappe.db.exists("Lead API Field", key):
			continue
		frappe.get_doc({
			"doctype": "Lead API Field",
			"field_key": key,
			"section_key": section,
			"fieldname": fieldname,
			"label": catalog_label(key),
		}).insert(ignore_permissions=True)
