# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Delete the Dashboard Charts that left the fixtures, which a fixture never deletes from a site that already holds them; a chart a Workspace or Dashboard places is kept."""
import frappe

# Replaced by Inbound by Path and Intake Faults per Day (reimport_external_leads_desk_declared_sources), or dropped with the integration lanes.
_RETIRED = (
	"API Errors (Daily)", "API Traffic (Daily)", "FB Leads per Day", "Intake Errors per Day",
	"Web Form Leads per Day", "Web Form Rejections per Day",
)


def execute():
	placed = set(frappe.get_all("Workspace Chart", pluck="chart_name")) | set(frappe.get_all("Dashboard Chart Link", pluck="chart"))
	for name in _RETIRED:
		if name in placed or not frappe.db.exists("Dashboard Chart", name):
			continue
		frappe.delete_doc("Dashboard Chart", name, force=True, ignore_permissions=True, ignore_missing=True)  # authz-ok: tier-a — patch, runs at migrate
