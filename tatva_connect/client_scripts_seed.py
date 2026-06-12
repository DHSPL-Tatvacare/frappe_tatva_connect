"""Ship Desk Client Scripts from per-module `<module>/client_scripts/*.js`. The JS is the
source of truth; this upserts each into a core `Client Script` record on every migrate.

Counterpart to form_scripts_seed (which targets the CRM SPA's `CRM Form Script`); this one
targets the Frappe **Desk** form (`/app/...`). Idempotent — keyed by the record name.
"""
import os

import frappe

# (Client Script name, dt, view, app-relative js path)
SCRIPTS = [
	("WhatsApp Account WATI Helpers", "WhatsApp Account", "Form", "whatsapp/client_scripts/whatsapp_account.js"),
]


def seed():
	base = frappe.get_app_path("tatva_connect")
	for name, dt, view, rel in SCRIPTS:
		path = os.path.join(base, rel)
		if not os.path.exists(path):
			continue
		with open(path) as f:
			js = f.read()
		doc = frappe.get_doc("Client Script", name) if frappe.db.exists("Client Script", name) \
			else frappe.new_doc("Client Script")
		if doc.is_new():
			doc.name = name
		doc.update({"dt": dt, "view": view, "enabled": 1, "script": js})
		doc.save(ignore_permissions=True)
	frappe.db.commit()
