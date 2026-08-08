"""Ship Desk Client Scripts from per-module `<module>/client_scripts/*.js`. The JS is the
source of truth; this upserts each into a core `Client Script` record on every migrate.

Counterpart to form_scripts_seed (which targets the CRM SPA's `CRM Form Script`); this one
targets the Frappe **Desk** form (`/app/...`). Idempotent — keyed by the record name.

EVERY seeded script is prefixed with `client_scripts_fallbacks.js`, and this is the ONE place that
happens. The shared helpers ride the content-hashed `public/js/tatva_connect.bundle.js`, which a
Client Script cannot depend on: a Client Script is served from the DB and runs whether or not that
asset resolved. When it did not, the helpers were undefined — and because each form calls one on the
FIRST line of its `refresh`, one missing asset took out every affordance on eight forms at once and
left a console line as the only evidence. Prefixing here fixes the class in one place; a guard in
eight scripts would have been the same rule written eight times, and the ninth form would forget it.
"""
import os

import frappe

# Old record names retired by a rename — deleted on migrate so a renamed record never
# double-binds its script alongside its predecessor.
_RETIRED = [
	"WhatsApp Account WATI Helpers",
	"WhatsApp Notification WATI Helpers",
	# CRM Facebook Settings is gone: one app was a Single, several apps are rows.
	"CRM Facebook Settings Helpers",
	# Held only the secret-reveal toggle, which is gone; a saved Password field stays masked.
	"CRM Facebook App Helpers",
]

# (Client Script name, dt, view, app-relative js path)
SCRIPTS = [
	("WhatsApp Account Webhook Helpers", "WhatsApp Account", "Form", "whatsapp/client_scripts/whatsapp_account.js"),
	("WhatsApp Notification Helpers", "WhatsApp Notification", "Form", "whatsapp/client_scripts/whatsapp_notification.js"),
	("CRM Telephony Account Webhook Helpers", "CRM Telephony Account", "Form", "telephony/client_scripts/telephony_account.js"),
	("CRM AI Voice Account Webhook Helpers", "CRM AI Voice Account", "Form", "voice/client_scripts/ai_voice_account.js"),
	("CRM Maps Settings Helpers", "CRM Maps Settings", "Form", "location/client_scripts/crm_maps_settings.js"),
	("CRM Push Settings Helpers", "CRM Push Settings", "Form", "notifications/client_scripts/push_settings.js"),
	("CRM Lead Activity Timeline", "CRM Lead", "Form", "activity/client_scripts/crm_lead_timeline.js"),
	("CRM Task Type Rules Helpers", "CRM Task Type", "Form", "taxonomy/client_scripts/crm_task_type.js"),
	("CRM Task Section Column Helpers", "CRM Task Section", "Form", "taxonomy/client_scripts/crm_task_section.js"),
	("CRM Intake Form Builder", "CRM Intake Form", "Form", "intake/client_scripts/crm_intake_form.js"),
	("Facebook Lead Form Mapping", "Facebook Lead Form", "Form", "lead_sync/client_scripts/facebook_lead_form.js"),
	("Lead Sync Source Token Helpers", "Lead Sync Source", "Form", "lead_sync/client_scripts/lead_sync_source.js"),
	("Facebook Page Token Helpers", "Facebook Page", "Form", "lead_sync/client_scripts/facebook_page.js"),
	("CRM Tatva Automation Description", "CRM Tatva Automation", "Form", "automation/client_scripts/crm_tatva_automation.js"),
	("CRM Tatva Automation Switch State (List)", "CRM Tatva Automation", "List", "automation/client_scripts/crm_tatva_automation_list.js"),
	("CRM Lead Import Helpers", "CRM Lead Import", "Form", "lead_import/client_scripts/crm_lead_import.js"),
	("Webhook Delivery Replay", "Integration Request", "Form", "webhooks/client_scripts/integration_request.js"),
	("Webhook Delivery Replay (List)", "Integration Request", "List", "webhooks/client_scripts/integration_request_list.js"),
]


# Prepended to every seeded script; declared as a file so the JS stays the source of truth, like the rest.
FALLBACKS = "client_scripts_fallbacks.js"


def seed():
	base = frappe.get_app_path("tatva_connect")
	fallbacks_path = os.path.join(base, FALLBACKS)
	if not os.path.exists(fallbacks_path):
		# Without it every seeded script loses its safety net silently, which is the defect this fixes.
		frappe.throw(f"Client Script fallbacks are missing: {FALLBACKS}")
	with open(fallbacks_path) as f:
		fallbacks = f.read()
	for name in _RETIRED:
		if frappe.db.exists("Client Script", name):
			frappe.delete_doc("Client Script", name, ignore_permissions=True)  # authz-ok: tier-a — seed, runs at migrate
	for name, dt, view, rel in SCRIPTS:
		path = os.path.join(base, rel)
		if not os.path.exists(path):
			# A declared file that isn't there is an unfinished rename; skipping it lost a whole Desk UI.
			frappe.throw(f"Client Script '{name}' declares a missing file: {rel}")
		if not frappe.db.exists("DocType", dt):
			# A script for a doctype this site does not have binds to nothing. Losing a Desk helper is a
			# gap; aborting post_schema_updates over one kills the whole deploy. Named, not silent.
			frappe.log_error(title="client_scripts_seed: target doctype missing", message=f"'{name}' targets {dt}, which is not installed; skipped.")
			continue
		with open(path) as f:
			js = f.read()
		doc = frappe.get_doc("Client Script", name) if frappe.db.exists("Client Script", name) \
			else frappe.new_doc("Client Script")
		if doc.is_new():
			doc.name = name
		doc.update({"dt": dt, "view": view, "enabled": 1, "script": f"{fallbacks}\n{js}"})
		doc.save(ignore_permissions=True)  # authz-ok: tier-a — seed, runs at migrate
	frappe.db.commit()
