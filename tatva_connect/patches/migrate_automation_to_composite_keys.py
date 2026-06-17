"""One-time: rename `CRM Tatva Automation` rows from flat keys to composite `::` keys.

The automation registry moved from flat keys (e.g. `wati`, `acefone`) to grain-scoped
composite primary keys (e.g. `WhatsApp::WATI::messaging`). This patch renames each existing
row in place, which PRESERVES every operator's tuned Enabled state across the PK rename
(the new seeder would otherwise re-seed switches dormant).

Idempotent: each rename skips if the old key is already gone or the new key already exists.
No-op on a fresh install — the old flat keys never existed there.
"""
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
