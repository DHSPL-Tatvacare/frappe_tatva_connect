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
	("WATI Failed Reason (CRM Lead)", "CRM Lead", "Form", "whatsapp/form_scripts/whatsapp_failed_reason.js"),
	# "Hide Status Pill (CRM Lead)" (hide_status_pill.js) RETIRED — lead lifecycle is now the native
	# grain-scoped stage pill (custom_stage) in the CRM fork; nothing to hide. Archived to
	# archive/lead-stage-retired-hide-status-pill/. Disabled idempotently via RETIRED below.
	("Data Tab Program Gate (CRM Lead)", "CRM Lead", "Form", "lead/form_scripts/data_tab_gate.js"),
	("Email Attach (CRM Lead)", "CRM Lead", "Form", "lead/form_scripts/email_attach.js"),
	("Delete Modal Fit (CRM Lead)", "CRM Lead", "Form", "lead/form_scripts/delete_modal_fit.js"),
	# "Log Activity (CRM Lead)" (activity_log.js) RETIRED in Phase 2 — the ad-hoc punch is now native in
	# the CRM fork (TatvaTasks owns window.__tcLogActivity → TatvaTaskModal create mode). Archived to
	# archive/phase2-retired-adhoc-punch/. Disabled idempotently via RETIRED below.
	("Near Me (CRM Lead)", "CRM Lead", "Form", "lead/form_scripts/near_me.js"),
	("Task Modal Fit (CRM Task)", "CRM Task", "Form", "tasks/form_scripts/task_modal_fit.js"),
	# v1 "Task Location Capture (CRM Task)" (task_location.js) RETIRED in Phase B — location folded
	# into the activity flow (save_activity + enforce_location). Archived to archive/phase-b-retired-v1-location/.
	("Activity Complete (CRM Task)", "CRM Task", "Form", "tasks/form_scripts/task_activity.js"),
]

# CRM Form Scripts we used to ship but have RETIRED. Disabled idempotently so a stale enabled record
# (its old flow) can never keep running after the .js is removed. The logic now lives in the CRM fork.
RETIRED = [
	"Log Activity (CRM Lead)",  # Phase 2: ad-hoc punch -> native TatvaTaskModal create flow (fork)
	"Hide Status Pill (CRM Lead)",  # lead stage native: grain-scoped stage pill (custom_stage) in the fork
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
	for name in RETIRED:
		if frappe.db.exists("CRM Form Script", name):
			frappe.db.set_value("CRM Form Script", name, "enabled", 0)
	frappe.db.commit()
