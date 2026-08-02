# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Shared fixture for the Facebook suites: every Lead Sync Source names the app that issued its token."""
import frappe

APP = "700000000000000"


def ensure_app(app_id=APP, secret="zz-app-secret"):
	"""The `CRM Facebook App` row a Facebook source cannot be saved without. Returns its name."""
	if not frappe.db.exists("CRM Facebook App", app_id):
		frappe.get_doc({
			"doctype": "CRM Facebook App", "app_id": app_id, "app_name": f"Probe {app_id}",
			"app_secret": secret, "graph_api_version": "v23.0", "lead_page_size": 100,
		}).insert(ignore_permissions=True)
	return app_id
