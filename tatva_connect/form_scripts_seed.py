"""Ship CRM Form Scripts from per-module `<module>/form_scripts/*.js`. The JS is the
source of truth; this upserts each into a CRM Form Script record on every migrate.
Doctype-scoped registry — no integration owns the home. Idempotent.
"""
import os

import frappe

# (CRM Form Script name, dt, view, app-relative js path)
SCRIPTS = [
	# All 4 WATI WhatsApp form scripts (whatsapp_template/gate/window/failed_reason.js) RETIRED — the
	# WhatsApp UI is now native first-class in the CRM fork (frontend/src/tatva/TatvaWhatsAppTemplate.vue
	# + TatvaWhatsAppWindowNotice.vue, plus failed-reason Tooltip / grain tab gate seams). Backend
	# (routing/WATI/api) unchanged — the native components call the same whitelisted endpoints. Archived
	# to archive/whatsapp-native-promotion/. Disabled idempotently via RETIRED below.
	# "Hide Status Pill (CRM Lead)" (hide_status_pill.js) RETIRED — lead lifecycle is now the native
	# grain-scoped stage pill (custom_stage) in the CRM fork; nothing to hide. Archived to
	# archive/lead-stage-retired-hide-status-pill/. Disabled idempotently via RETIRED below.
	# "Data Tab Program Gate (CRM Lead)" (data_tab_gate.js) RETIRED — the Data tab is now the native
	# grain/brain-aware panel in the CRM fork (frontend/src/tatva/DetailPanel.vue, fed by
	# tatva_connect.lead.detail.lead_detail). Drug-vs-metabolic section applicability is resolved
	# server-side in the projection (the program "world"), so the DOM section-hiding hack is gone.
	# Archived to archive/lead-detail-native-promotion/. Disabled idempotently via RETIRED below.
	("Email Attach (CRM Lead)", "CRM Lead", "Form", "lead/form_scripts/email_attach.js"),
	("Delete Modal Fit (CRM Lead)", "CRM Lead", "Form", "lead/form_scripts/delete_modal_fit.js"),
	# "Log Activity (CRM Lead)" (activity_log.js) RETIRED in Phase 2 — the ad-hoc punch is now native in
	# the CRM fork (TatvaTasks owns window.__tcLogActivity → TatvaTaskModal create mode). Archived to
	# archive/phase2-retired-adhoc-punch/. Disabled idempotently via RETIRED below.
	# "Near Me (CRM Lead)" (near_me.js) RETIRED — the header-button hack is now a native first-class
	# screen in the CRM fork (pages/NearMe.vue: left-menu item → map + cross-grain doctor directory,
	# gated by the Field Map User role + Location::NearMe::directory switch). Backend is
	# tatva_connect/near_me/api.py. Archived to archive/near-me-native-promotion/. Disabled via RETIRED below.
	# "Task Modal Fit (CRM Task)" (task_modal_fit.js) RETIRED — the DOM/MutationObserver height hijack on
	# crm's DoctypeModal is gone: the native TaskModal (fork tatva/TaskModal.vue) is the one task modal
	# (create/edit/view/complete) with its own contained body + internal scroll. Disabled via RETIRED below.
	# "Activity Complete (CRM Task)" (task_activity.js) RETIRED — the form-script activity+location flow
	# (onBeforeCreate/onValidate) now lives in TaskModal: pick a grain-scoped type → schema fields →
	# resolveLocation (only when the type needs it) → compute_activity_fields/save_activity. Same server
	# brains + enforce_* backstops. Disabled via RETIRED below.
]

# CRM Form Scripts we used to ship but have RETIRED. Disabled idempotently so a stale enabled record
# (its old flow) can never keep running after the .js is removed. The logic now lives in the CRM fork.
RETIRED = [
	"Log Activity (CRM Lead)",  # Phase 2: ad-hoc punch -> native TatvaTaskModal create flow (fork)
	"Hide Status Pill (CRM Lead)",  # lead stage native: grain-scoped stage pill (custom_stage) in the fork
	# WhatsApp UI promoted to native first-class components in the CRM fork (backend untouched):
	"WATI Send Template (CRM Lead)",  # -> TatvaWhatsAppTemplate.vue + header split button (Send Template / Refresh History)
	"WATI WhatsApp Gate (CRM Lead)",  # -> native grain-routed tab gate (whatsappRouted composable)
	"WATI WhatsApp Window (CRM Lead)",  # -> TatvaWhatsAppWindowNotice.vue in WhatsAppBox (24h window-closed card)
	"WATI Failed Reason (CRM Lead)",  # -> native Tooltip on the failed Badge in WhatsAppArea.vue
	"Task Location Capture (CRM Task)",  # Phase B: v1 location folded into the activity flow (task_activity.js + save_activity/enforce_location)
	"Near Me (CRM Lead)",  # -> native Near Me page (fork pages/NearMe.vue) + tatva_connect/near_me/api.py
	"Data Tab Program Gate (CRM Lead)",  # -> native Data tab (fork tatva/DetailPanel.vue) + tatva_connect/lead/detail.py; applicability resolved server-side
	"Task Modal Fit (CRM Task)",  # -> native TaskModal contained body + internal scroll (fork tatva/TaskModal.vue); DOM height hijack gone
	"Activity Complete (CRM Task)",  # -> native TaskModal activity flow (grain-scoped type -> schema -> save_activity/compute_activity_fields); enforce_* backstops kept
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
