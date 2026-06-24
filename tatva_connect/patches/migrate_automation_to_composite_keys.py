"""Rename CRM Tatva Automation rows from flat keys to composite `::` keys in place, preserving each operator's tuned Enabled state across the PK rename; idempotent."""
import frappe

_RENAMES = {
	"wati": "WhatsApp::WATI::messaging",
	"template_sync": "WhatsApp::WATI::templates",
	"acefone": "Telephony::Acefone::calls",
	"azure": "Storage::Azure::offload",
	"location": "Location::Google::capture",
	"activity_transitions": "Task::CRM Task::transitions",
	"followup": "Task::Assignment::followup",
	"draft_cleanup": "Storage::File::draft-cleanup",
	"lead_hygiene": "Lead::CRM Lead::dedup",
	"task_guards": "Task::CRM Task::guards",
	"file_privacy": "Storage::File::privacy",
	"headline_sync": "Lead::CRM Lead::headline",
	"intake": "Lead::Enrolment::intake",
	"catalog_cache": "Partner::Catalog::cache",
}


def execute():
	for old, new in _RENAMES.items():
		if frappe.db.exists("CRM Tatva Automation", old) and not frappe.db.exists("CRM Tatva Automation", new):
			frappe.rename_doc("CRM Tatva Automation", old, new, force=True)
	frappe.db.commit()
