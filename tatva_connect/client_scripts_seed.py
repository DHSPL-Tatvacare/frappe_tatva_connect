"""Ship Desk Client Scripts from per-module `<module>/client_scripts/*.js`. The JS is the
source of truth; this upserts each into a core `Client Script` record on every migrate.

Counterpart to form_scripts_seed (which targets the CRM SPA's `CRM Form Script`); this one
targets the Frappe **Desk** form (`/app/...`). Idempotent — keyed by the record name.
"""
import os

import frappe

# Old record names retired by a rename — deleted on migrate so a renamed record never
# double-binds its script alongside its predecessor.
_RETIRED = [
	"WhatsApp Account WATI Helpers",
	"WhatsApp Notification WATI Helpers",
]

# (Client Script name, dt, view, app-relative js path)
SCRIPTS = [
	("WhatsApp Account Webhook Helpers", "WhatsApp Account", "Form", "whatsapp/client_scripts/whatsapp_account.js"),
	("WhatsApp Notification Helpers", "WhatsApp Notification", "Form", "whatsapp/client_scripts/whatsapp_notification.js"),
	("CRM Telephony Account Webhook Helpers", "CRM Telephony Account", "Form", "telephony/client_scripts/telephony_account.js"),
	("CRM Maps Settings Helpers", "CRM Maps Settings", "Form", "location/client_scripts/crm_maps_settings.js"),
	("CRM Lead Activity Timeline", "CRM Lead", "Form", "activity/client_scripts/crm_lead_timeline.js"),
]


def seed():
	base = frappe.get_app_path("tatva_connect")
	for name in _RETIRED:
		if frappe.db.exists("Client Script", name):
			frappe.delete_doc("Client Script", name, ignore_permissions=True)
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
