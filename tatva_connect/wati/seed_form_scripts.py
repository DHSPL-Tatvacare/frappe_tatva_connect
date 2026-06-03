"""Ship CRM Form Scripts from their .js source files (schema-as-code).

crm loads `CRM Form Script` records from the DB; the JS source-of-truth lives in
`wati/form_scripts/*.js`. This upserts each into a CRM Form Script record on every
migrate, so the scripts ship with the app and stay in sync (no manual seeding, no
drift). Idempotent.
"""
import os

import frappe

# (CRM Form Script name, dt, view, js filename under wati/form_scripts/)
SCRIPTS = [
	("WATI Send Template (CRM Lead)", "CRM Lead", "Form", "lead_whatsapp_template.js"),
	("WATI WhatsApp Gate (CRM Lead)", "CRM Lead", "Form", "lead_whatsapp_gate.js"),
]


def seed():
	base = frappe.get_app_path("tatva_connect", "wati", "form_scripts")
	for name, dt, view, fname in SCRIPTS:
		path = os.path.join(base, fname)
		if not os.path.exists(path):
			continue
		with open(path) as f:
			js = f.read()
		if frappe.db.exists("CRM Form Script", name):
			doc = frappe.get_doc("CRM Form Script", name)
		else:
			doc = frappe.new_doc("CRM Form Script")
			doc.name = name  # CRM Form Script is prompt-named
		doc.update({"dt": dt, "view": view, "enabled": 1, "script": js})
		doc.save(ignore_permissions=True)
	frappe.db.commit()
