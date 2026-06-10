"""Ship CRM Form Scripts from per-module `<module>/form_scripts/*.js`. The JS is the
source of truth; this upserts each into a CRM Form Script record on every migrate.
Doctype-scoped registry — no integration owns the home. Idempotent.
"""
import os

import frappe

# (CRM Form Script name, dt, view, app-relative js path)
SCRIPTS = [
	("WATI Send Template (CRM Lead)", "CRM Lead", "Form", "whatsapp/form_scripts/whatsapp_template.js"),
	("WATI WhatsApp Gate (CRM Lead)", "CRM Lead", "Form", "whatsapp/form_scripts/whatsapp_gate.js"),
	("WATI WhatsApp Window (CRM Lead)", "CRM Lead", "Form", "whatsapp/form_scripts/whatsapp_window.js"),
	("Hide Status Pill (CRM Lead)", "CRM Lead", "Form", "lead/form_scripts/hide_status_pill.js"),
	("Data Tab Program Gate (CRM Lead)", "CRM Lead", "Form", "lead/form_scripts/data_tab_gate.js"),
	("Task Modal Fit (CRM Task)", "CRM Task", "Form", "tasks/form_scripts/task_modal_fit.js"),
]


def seed():
	base = frappe.get_app_path("tatva_connect")
	for name, dt, view, rel in SCRIPTS:
		path = os.path.join(base, rel)
		if not os.path.exists(path):
			continue
		with open(path) as f:
			js = f.read()
		doc = frappe.get_doc("CRM Form Script", name) if frappe.db.exists("CRM Form Script", name) \
			else frappe.new_doc("CRM Form Script")
		if doc.is_new():
			doc.name = name
		doc.update({"dt": dt, "view": view, "enabled": 1, "script": js})
		doc.save(ignore_permissions=True)
	frappe.db.commit()
