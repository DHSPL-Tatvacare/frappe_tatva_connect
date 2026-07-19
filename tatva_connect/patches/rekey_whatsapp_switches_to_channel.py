"""Land the WhatsApp switches on `WhatsApp::Channel::{messaging,templates,backfill}`.

A switch is a property of the CHANNEL, not of whoever carries it. Keyed by vendor
(`WhatsApp::WATI::x`), an operator who changed provider would silently lose their own configuration —
the new vendor's key defaults OFF, so WhatsApp would go dark on the migrate that introduced it, with
nothing in the log to say why. The per-vendor control is the one that genuinely belongs to a vendor:
the account's Active/Inactive status.

Every key on this site is `Area::Subject::Capability`, so `Channel` is the subject — these gate the
whole channel, not one vendor. An interim revision of this patch landed two-part `WhatsApp::x` keys,
which is why BOTH the vendor-scoped originals and those two-part names are accepted as starting
points: a bench that already ran the interim patch and a bench that never did must reach the same
place.

End state, whatever ran before: exactly the three `WhatsApp::Channel::*` rows exist, each carrying
whatever `enabled` the operator had tuned on its predecessor, with no `WhatsApp::WATI::*` or two-part
`WhatsApp::*` row left behind and no `requires` pointing at a dead key. Safe to run twice, and a
no-op on a site that never had either predecessor (a fresh install seeds the new keys from the
registry).
"""
import frappe

# Both predecessors map onto the same destination — whichever exists is carried across.
_REKEY = {
	"WhatsApp::WATI::messaging": "WhatsApp::Channel::messaging",
	"WhatsApp::WATI::templates": "WhatsApp::Channel::templates",
	"WhatsApp::WATI::backfill": "WhatsApp::Channel::backfill",
	"WhatsApp::messaging": "WhatsApp::Channel::messaging",
	"WhatsApp::templates": "WhatsApp::Channel::templates",
	"WhatsApp::backfill": "WhatsApp::Channel::backfill",
}


def execute():
	for old, new in _REKEY.items():
		if not frappe.db.exists("CRM Tatva Automation", old):
			continue
		if frappe.db.exists("CRM Tatva Automation", new):
			# Both exist: the registry seed already created the new row, so the operator's tuned state
			# lives on the OLD one. Carry it across, then drop the duplicate gate.
			enabled = frappe.db.get_value("CRM Tatva Automation", old, "enabled")
			frappe.db.set_value("CRM Tatva Automation", new, "enabled", enabled)
			frappe.delete_doc("CRM Tatva Automation", old, force=True, ignore_permissions=True)  # authz-ok: tier-c — patch, no user context
			continue
		frappe.rename_doc("CRM Tatva Automation", old, new, force=True)

	# The `requires` column names another row by key, so a rename that left it pointing at a key that no
	# longer exists would break the dependency chain the settings screen draws.
	for old, new in _REKEY.items():
		frappe.db.set_value(
			"CRM Tatva Automation", {"requires": old}, "requires", new, update_modified=False
		)
	frappe.db.commit()
